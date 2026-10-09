"""
Standalone XAUUSD Market Update poster.

Separate from forex_alert.py by design. Posts a narrative structure chart
to Telegram. Skips weekends (same guard as forex_alert.py).

Feed: Twelve Data XAU/USD spot (TWELVE_DATA_API_KEY).

What it does each run:
  1. Fetches the main timeframe (default 30min) AND a higher timeframe
     (default 4h) with retries, and skips the post if the last candle is stale.
  2. Structure: swings, BOS vs CHoCH, displacement, order blocks.
  3. Market phase: Continuation / Reversal / Pullback / Trend Leg /
     Invalidation Risk / Potential Reversal / Range, with a confirmation
     status (displacement + retest).
  4. HTF bias and whether the current read is aligned with it or counter-trend.
  5. Fair value gaps (unfilled / partly filled), premium/discount (50% line),
     key levels (previous day high/low, Asia range, equal highs/lows) and
     liquidity sweeps of those levels.
  6. Session tag, invalidation level, and an informational trade idea
     (entry zone, stop, 1R/2R/3R).
  7. Skips near-identical repeat posts (state file), with a heartbeat post
     every FORCE_POST_HOURS so the channel never goes silent for too long.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime, timezone, timedelta

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

PAIR = os.getenv("XAU_PAIR", "XAU/USD")
INTERVAL = os.getenv("XAU_INTERVAL", "30min")
OUTPUT_SIZE = int(os.getenv("XAU_OUTPUTSIZE", "150"))   # raise this on 15min/5min so "previous day" is fully covered
HTF_INTERVAL = os.getenv("XAU_HTF_INTERVAL", "4h")
HTF_OUTPUTSIZE = int(os.getenv("XAU_HTF_OUTPUTSIZE", "120"))

SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "3"))
TREND_LOOKBACK_SWINGS = int(os.getenv("TREND_LOOKBACK_SWINGS", "4"))
OB_LOOKBACK = int(os.getenv("OB_LOOKBACK", "15"))
DISPLACEMENT_ATR_MULT = float(os.getenv("DISPLACEMENT_ATR_MULT", "1.0"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
PROJECTION_ATR_MULT = float(os.getenv("PROJECTION_ATR_MULT", "2.0"))

# phase classification
PULLBACK_ATR_MULT = float(os.getenv("PULLBACK_ATR_MULT", "0.75"))
RETEST_WINDOW = int(os.getenv("RETEST_WINDOW", "8"))
RETEST_TOL_ATR_MULT = float(os.getenv("RETEST_TOL_ATR_MULT", "0.3"))
SWEEP_WINDOW = int(os.getenv("SWEEP_WINDOW", "6"))

# FVG / levels
FVG_MIN_ATR_MULT = float(os.getenv("FVG_MIN_ATR_MULT", "0.3"))   # ignore tiny gaps
FVG_LOOKBACK = int(os.getenv("FVG_LOOKBACK", "60"))
FVG_MAX_ZONES = int(os.getenv("FVG_MAX_ZONES", "3"))
EQ_TOL_ATR_MULT = float(os.getenv("EQ_TOL_ATR_MULT", "0.15"))    # how close two swings must be to count as "equal"
ASIA_START_HOUR = int(os.getenv("ASIA_START_HOUR", "0"))         # UTC
ASIA_END_HOUR = int(os.getenv("ASIA_END_HOUR", "7"))             # UTC

# trade idea
SL_BUFFER_ATR_MULT = float(os.getenv("SL_BUFFER_ATR_MULT", "0.3"))
MIN_RISK_ATR_MULT = float(os.getenv("MIN_RISK_ATR_MULT", "0.2"))
MAX_RISK_ATR_MULT = float(os.getenv("MAX_RISK_ATR_MULT", "5.0"))

# repeat-skipping
SKIP_REPEATS = os.getenv("SKIP_REPEATS", "true").lower() == "true"
FORCE_POST_HOURS = float(os.getenv("FORCE_POST_HOURS", "4"))     # 0 = never force a heartbeat post
STATE_FILE = os.getenv("XAU_STATE_FILE", "state_xau_update.json")

# data safety
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "3"))
STALE_BARS = int(os.getenv("STALE_BARS", "3"))                   # skip if last candle is older than this many bars

PHASE_COLORS = {
    "Continuation": "#2e7d32",
    "Reversal": "#d500f9",
    "Pullback": "#f57c00",
    "Trend Leg": "#1565c0",
    "Invalidation Risk": "#b71c1c",
    "Potential Reversal": "#6a1b9a",
    "Range": "#616161",
}


# ================== HELPERS ==================
def interval_minutes(interval):
    s = interval.lower().strip()
    if s.endswith("min"):
        return int(s[:-3])
    if s.endswith("h"):
        return int(s[:-1]) * 60
    if s.endswith("day"):
        return int(s[:-3] or 1) * 1440
    return 30


def now_utc():
    return datetime.now(timezone.utc)


# ================== MARKET HOURS ==================
def is_forex_market_open():
    """Closed all day Saturday, closed Friday from 22:00 UTC, closed
    Sunday until 22:00 UTC. Same rule as forex_alert.py."""
    now = now_utc()
    weekday, hour = now.weekday(), now.hour
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 22:
        return False
    if weekday == 6 and hour < 22:
        return False
    return True


# ================== SESSION ==================
def current_session():
    h = now_utc().hour
    if 0 <= h < 7:
        return "Asia", "quieter session, price often ranges and builds liquidity"
    if 7 <= h < 12:
        return "London", "volatility picks up, the Asia range is often swept"
    if 12 <= h < 16:
        return "London/NY overlap", "highest volume, biggest moves"
    if 16 <= h < 21:
        return "New York", "continuation or reversal after the overlap"
    return "Late NY / rollover", "thin liquidity, wider spreads"


# ================== LIVE DATA (Twelve Data, spot XAU/USD) ==================
def fetch_candles(pair, interval, outputsize):
    """Fetch candles with retries. Raises RuntimeError if every attempt fails."""
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not set.")

    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "timezone": "UTC",
    })

    data = None
    last_err = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                data = json.loads(resp.read().decode())
            if data.get("values"):
                break
            last_err = RuntimeError(data.get("message", data))
        except Exception as e:
            last_err = e
        data = None
        if attempt < FETCH_RETRIES:
            time.sleep(RETRY_DELAY * attempt)

    if data is None:
        raise RuntimeError(f"Twelve Data failed [{pair} {interval}] after {FETCH_RETRIES} attempts: {last_err}")

    rows = list(reversed(data["values"]))  # oldest -> newest
    df = pd.DataFrame({
        "Open": [float(r["open"]) for r in rows],
        "High": [float(r["high"]) for r in rows],
        "Low": [float(r["low"]) for r in rows],
        "Close": [float(r["close"]) for r in rows],
    }, index=pd.to_datetime([r["datetime"] for r in rows]))

    df = df.dropna()
    if len(df) < 30:
        raise ValueError(f"Not enough candles received ({len(df)})")
    return df


def get_live_data(pair=PAIR, interval=INTERVAL, outputsize=OUTPUT_SIZE):
    df = fetch_candles(pair, interval, outputsize)
    return df, round(df["Close"].iloc[-1], 2)


def check_fresh(df):
    """The last candle must be recent. Returns (is_fresh, age_minutes)."""
    now = now_utc().replace(tzinfo=None)
    age_min = (now - df.index[-1].to_pydatetime()).total_seconds() / 60
    limit = STALE_BARS * interval_minutes(INTERVAL)
    return age_min <= limit, age_min


# ================== INDICATORS ==================
def atr(df, period=ATR_PERIOD):
    highs, lows, closes = df["High"].values, df["Low"].values, df["Close"].values
    n = len(closes)
    if n <= period:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    avg = sum(trs[:period]) / period
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


# ================== SWING DETECTION ==================
def detect_swings(df, left=SWING_LOOKBACK, right=SWING_LOOKBACK):
    df = df.copy()
    df["Swing_High"] = np.nan
    df["Swing_Low"] = np.nan

    for i in range(left, len(df) - right):
        if df["High"].iloc[i] == df["High"].iloc[i - left:i + right + 1].max():
            df.loc[df.index[i], "Swing_High"] = df["High"].iloc[i]
        if df["Low"].iloc[i] == df["Low"].iloc[i - left:i + right + 1].min():
            df.loc[df.index[i], "Swing_Low"] = df["Low"].iloc[i]
    return df


def find_order_block(df, bias, before_idx, lookback=OB_LOOKBACK):
    """Last opposite-colored candle before the impulsive breakout leg."""
    opens, closes = df["Open"].values, df["Close"].values
    highs, lows = df["High"].values, df["Low"].values
    start = max(0, before_idx - lookback)
    for i in range(before_idx - 1, start - 1, -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]
        if (bias == "Bullish" and is_bearish) or (bias == "Bearish" and is_bullish):
            return {"high": highs[i], "low": lows[i]}
    return None


def find_order_blocks_multi(df, bias, swings, lookback=OB_LOOKBACK, max_zones=3):
    zones = []
    for idx in swings.tail(max_zones).index:
        before_idx = df.index.get_loc(idx)
        ob = find_order_block(df, bias, before_idx=before_idx, lookback=lookback)
        if ob and ob not in zones:
            zones.append(ob)
    return zones


def analyze_structure_deep(df):
    """Bias needs the last-two swing highs AND last-two swing lows to agree
    (both lower = Bearish, both higher = Bullish); disagreement = Neutral
    range. BOS vs CHoCH compares the latest break against the trend implied
    by earlier swings. Displacement: breaking candle range >=
    DISPLACEMENT_ATR_MULT x ATR.

    Returns (structure_notes, bias, break_kind, break_index, break_level,
    swings_high, swings_low, order_block, displacement_hit, atr_val).
    """
    df = detect_swings(df)
    swings_high_all = df[df["Swing_High"].notna()][["Swing_High"]]
    swings_low_all = df[df["Swing_Low"].notna()][["Swing_Low"]]
    atr_val = atr(df)

    swings_high = swings_high_all.tail(4)
    swings_low = swings_low_all.tail(4)

    high_dir = None
    low_dir = None
    if len(swings_high) >= 2:
        high_dir = "lower" if swings_high["Swing_High"].iloc[-1] < swings_high["Swing_High"].iloc[-2] else "higher"
    if len(swings_low) >= 2:
        low_dir = "lower" if swings_low["Swing_Low"].iloc[-1] < swings_low["Swing_Low"].iloc[-2] else "higher"

    structure_notes = []
    bias = "Neutral"

    if high_dir and low_dir:
        if high_dir == "lower" and low_dir == "lower":
            bias = "Bearish"
            structure_notes.append("Lower High confirmed")
            structure_notes.append("Lower Low")
        elif high_dir == "higher" and low_dir == "higher":
            bias = "Bullish"
            structure_notes.append("Higher High")
            structure_notes.append("Higher Low")
        elif high_dir == "lower" and low_dir == "higher":
            bias = "Neutral"
            structure_notes.append("Lower High confirmed")
            structure_notes.append("Higher Low")
            structure_notes.append("Contracting range — no clear trend")
        else:
            bias = "Neutral"
            structure_notes.append("Higher High")
            structure_notes.append("Lower Low")
            structure_notes.append("Expanding range — no clear trend")
    elif high_dir:
        bias = "Bearish" if high_dir == "lower" else "Bullish"
        structure_notes.append("Lower High confirmed" if high_dir == "lower" else "Higher High")
    elif low_dir:
        bias = "Bullish" if low_dir == "higher" else "Bearish"
        structure_notes.append("Higher Low" if low_dir == "higher" else "Lower Low")

    # prior trend from a longer swing lookback, to decide BOS vs CHoCH
    prior_bias = "Neutral"
    hh_prior = swings_high_all.tail(TREND_LOOKBACK_SWINGS + 1)
    ll_prior = swings_low_all.tail(TREND_LOOKBACK_SWINGS + 1)
    if len(hh_prior) >= TREND_LOOKBACK_SWINGS and len(ll_prior) >= TREND_LOOKBACK_SWINGS:
        hh_excl_latest = hh_prior.iloc[:-1] if len(hh_prior) > TREND_LOOKBACK_SWINGS else hh_prior
        ll_excl_latest = ll_prior.iloc[:-1] if len(ll_prior) > TREND_LOOKBACK_SWINGS else ll_prior
        if len(hh_excl_latest) >= 2 and hh_excl_latest["Swing_High"].iloc[-1] > hh_excl_latest["Swing_High"].iloc[-2]:
            prior_bias = "Bullish"
        elif len(hh_excl_latest) >= 2:
            prior_bias = "Bearish"
        if prior_bias == "Neutral" and len(ll_excl_latest) >= 2:
            prior_bias = "Bullish" if ll_excl_latest["Swing_Low"].iloc[-1] > ll_excl_latest["Swing_Low"].iloc[-2] else "Bearish"

    break_kind = None
    break_index = None
    break_level = None
    order_block = None
    displacement_hit = False

    if bias in ("Bullish", "Bearish") and (len(swings_high) >= 1 or len(swings_low) >= 1):
        n = len(df)
        last_close = df["Close"].iloc[-1]
        if bias == "Bullish" and len(swings_high) >= 1:
            break_level = swings_high["Swing_High"].iloc[-1]
            break_index = df.index.get_loc(swings_high.index[-1])
        elif bias == "Bearish" and len(swings_low) >= 1:
            break_level = swings_low["Swing_Low"].iloc[-1]
            break_index = df.index.get_loc(swings_low.index[-1])

        if break_level is not None:
            broke = (last_close > break_level) if bias == "Bullish" else (last_close < break_level)
            if broke:
                last_range = df["High"].iloc[-1] - df["Low"].iloc[-1]
                displacement_hit = bool(atr_val) and last_range >= DISPLACEMENT_ATR_MULT * atr_val
                break_kind = "CHoCH" if bias != prior_bias and prior_bias != "Neutral" else "BOS"
                order_block = find_order_block(df, bias, before_idx=n - 1, lookback=OB_LOOKBACK)
                structure_notes.append(
                    f"{break_kind} confirmed" + (" with displacement" if displacement_hit else " (no displacement)")
                )
            else:
                break_level = None

    return (structure_notes, bias, break_kind, break_index, break_level,
            swings_high, swings_low, order_block, displacement_hit, atr_val)


# ================== HIGHER TIMEFRAME ==================
def get_htf_context():
    """Higher-timeframe bias. Returns None if the HTF fetch fails (the
    post still goes out, just without the HTF line)."""
    try:
        htf_df = fetch_candles(PAIR, HTF_INTERVAL, HTF_OUTPUTSIZE)
        res = analyze_structure_deep(htf_df)
        return {"bias": res[1], "interval": HTF_INTERVAL.upper(), "notes": res[0]}
    except Exception as e:
        print("HTF fetch failed (continuing without it):", e)
        return None


def htf_alignment(bias, htf):
    """Returns (state, text). state: aligned / counter / neutral / unavailable."""
    if not htf:
        return "unavailable", "HTF bias unavailable"
    hb = htf["bias"]
    tf = htf["interval"]
    if hb == "Neutral":
        return "neutral", f"{tf} is ranging, so there is no higher-timeframe edge"
    if bias == "Neutral":
        return "neutral", f"{tf} bias is {hb}"
    if bias == hb:
        return "aligned", f"Aligned with {tf} {hb} bias"
    return "counter", f"Counter-trend vs {tf} {hb} bias, so lower conviction"


# ================== KEY LEVELS / LIQUIDITY ==================
def compute_key_levels(df):
    """Previous day high/low and the Asia session range (UTC)."""
    levels = []
    idx = df.index
    dates = idx.normalize()
    today = dates[-1]

    prev_dates = dates[dates < today]
    if len(prev_dates):
        prev_date = prev_dates[-1]
        # only trust PDH/PDL if our data window actually covers that whole day
        if idx[0] <= prev_date + timedelta(hours=1):
            mask = np.asarray(dates == prev_date)
            day = df[mask]
            last_pos = int(np.where(mask)[0][-1])
            levels.append({"name": "PDH", "price": float(day["High"].max()), "kind": "high", "pos": last_pos})
            levels.append({"name": "PDL", "price": float(day["Low"].min()), "kind": "low", "pos": last_pos})

    hours = idx.hour
    asia_mask = np.asarray((hours >= ASIA_START_HOUR) & (hours < ASIA_END_HOUR))
    if asia_mask.any():
        asia = df[asia_mask]
        last_date = asia.index.normalize()[-1]
        sub = asia[np.asarray(asia.index.normalize() == last_date)]
        if len(sub) >= 2:
            pos = df.index.get_loc(sub.index[-1])
            levels.append({"name": "Asia H", "price": float(sub["High"].max()), "kind": "high", "pos": pos})
            levels.append({"name": "Asia L", "price": float(sub["Low"].min()), "kind": "low", "pos": pos})
    return levels


def find_equal_levels(df, atr_val):
    """Equal highs / equal lows: two recent swings within a small ATR
    tolerance of each other (resting liquidity). Most recent pair only."""
    if not atr_val:
        return []
    tol = atr_val * EQ_TOL_ATR_MULT
    d = detect_swings(df)
    out = []

    def scan(series, kind, name):
        items = list(series.items())  # oldest -> newest
        for i in range(len(items) - 1, 0, -1):
            for j in range(i - 1, -1, -1):
                if abs(items[i][1] - items[j][1]) <= tol:
                    return {"name": name, "price": float((items[i][1] + items[j][1]) / 2),
                            "kind": kind, "pos": df.index.get_loc(items[i][0])}
        return None

    eqh = scan(d[d["Swing_High"].notna()]["Swing_High"].tail(6), "high", "EQH")
    eql = scan(d[d["Swing_Low"].notna()]["Swing_Low"].tail(6), "low", "EQL")
    if eqh:
        out.append(eqh)
    if eql:
        out.append(eql)
    return out


def build_levels(df, swings_high, swings_low, atr_val):
    """Returns (all_levels_for_sweep_detection, key_levels_for_chart)."""
    swing_levels = []
    if len(swings_high):
        swing_levels.append({"name": "swing high", "price": float(swings_high["Swing_High"].iloc[-1]),
                             "kind": "high", "pos": df.index.get_loc(swings_high.index[-1]), "key": False})
    if len(swings_low):
        swing_levels.append({"name": "swing low", "price": float(swings_low["Swing_Low"].iloc[-1]),
                             "kind": "low", "pos": df.index.get_loc(swings_low.index[-1]), "key": False})
    key_levels = compute_key_levels(df) + find_equal_levels(df, atr_val)
    for k in key_levels:
        k["key"] = True
    return swing_levels + key_levels, key_levels


def detect_liquidity_sweep(df, levels):
    """A wick through a level that closes back inside it within the last
    SWEEP_WINDOW bars, with price still on the inside now. The most recent
    sweep wins; on a tie a key level beats a plain swing."""
    n = len(df)
    close = df["Close"].iloc[-1]
    best = None
    for lv in levels:
        start = max(lv["pos"] + 1, n - SWEEP_WINDOW)
        if start >= n:
            continue
        win = df.iloc[start:]
        if lv["kind"] == "high":
            mask = ((win["High"] > lv["price"]) & (win["Close"] < lv["price"])).values
            if mask.any() and close < lv["price"]:
                cand = {"name": lv["name"], "side": "buy-side", "level": lv["price"],
                        "pos": start + int(np.where(mask)[0][-1]), "dir": "bearish",
                        "extreme": float(win["High"].max()), "key": lv.get("key", False)}
            else:
                continue
        else:
            mask = ((win["Low"] < lv["price"]) & (win["Close"] > lv["price"])).values
            if mask.any() and close > lv["price"]:
                cand = {"name": lv["name"], "side": "sell-side", "level": lv["price"],
                        "pos": start + int(np.where(mask)[0][-1]), "dir": "bullish",
                        "extreme": float(win["Low"].min()), "key": lv.get("key", False)}
            else:
                continue
        if best is None or (cand["pos"], cand["key"]) > (best["pos"], best["key"]):
            best = cand
    return best


# ================== FVG / PREMIUM-DISCOUNT ==================
def detect_fvgs(df, atr_val, lookback=FVG_LOOKBACK, max_zones=FVG_MAX_ZONES):
    """3-candle imbalances that haven't been fully filled. 'filled' is the
    fraction of the gap price has already traded back into."""
    hi, lo = df["High"].values, df["Low"].values
    n = len(df)
    min_size = (atr_val or 0) * FVG_MIN_ATR_MULT
    found = []
    for i in range(max(2, n - lookback), n):
        # bullish gap: this candle's low is above the high from two candles ago
        if lo[i] > hi[i - 2] and (lo[i] - hi[i - 2]) >= min_size:
            bottom, top = float(hi[i - 2]), float(lo[i])
            if i + 1 < n:
                later_low = float(lo[i + 1:].min())
                if later_low <= bottom:
                    continue  # fully filled
                filled = (top - later_low) / (top - bottom) if later_low < top else 0.0
            else:
                filled = 0.0
            found.append({"kind": "bullish", "top": top, "bottom": bottom, "pos": i - 1, "filled": filled})
        # bearish gap: this candle's high is below the low from two candles ago
        if hi[i] < lo[i - 2] and (lo[i - 2] - hi[i]) >= min_size:
            top, bottom = float(lo[i - 2]), float(hi[i])
            if i + 1 < n:
                later_high = float(hi[i + 1:].max())
                if later_high >= top:
                    continue
                filled = (later_high - bottom) / (top - bottom) if later_high > bottom else 0.0
            else:
                filled = 0.0
            found.append({"kind": "bearish", "top": top, "bottom": bottom, "pos": i - 1, "filled": filled})
    return found[-max_zones:]


def premium_discount(price, swings_high, swings_low):
    if not len(swings_high) or not len(swings_low):
        return None
    hi = float(swings_high["Swing_High"].tail(2).max())
    lo = float(swings_low["Swing_Low"].tail(2).min())
    if hi <= lo:
        return None
    pct = (price - lo) / (hi - lo)
    if pct > 1:
        label = "Above range"
    elif pct < 0:
        label = "Below range"
    elif pct > 0.55:
        label = "Premium"
    elif pct < 0.45:
        label = "Discount"
    else:
        label = "Equilibrium"
    return {"hi": hi, "lo": lo, "eq": (hi + lo) / 2, "pct": pct, "label": label}


# ================== PHASE CLASSIFICATION ==================
def classify_phase(df, bias, break_kind, break_level, swings_high, swings_low,
                   displacement_hit, atr_val, levels):
    """Returns phase, confirmation text, retracement depth and sweep info."""
    close = df["Close"].iloc[-1]
    a = atr_val or 0.0
    tol = a * RETEST_TOL_ATR_MULT

    sweep = detect_liquidity_sweep(df, levels)

    retest_held = False
    if break_kind and break_level is not None and len(df) > RETEST_WINDOW + 1:
        recent = df.iloc[-RETEST_WINDOW - 1:-1]
        if bias == "Bullish":
            touched = (recent["Low"] <= break_level + tol) & (recent["Close"] >= break_level)
            retest_held = bool(touched.any() and close > break_level)
        elif bias == "Bearish":
            touched = (recent["High"] >= break_level - tol) & (recent["Close"] <= break_level)
            retest_held = bool(touched.any() and close < break_level)

    depth = None
    depth_label = None
    pull_dist = 0.0
    if bias in ("Bullish", "Bearish") and len(swings_high) and len(swings_low):
        last_sh = swings_high["Swing_High"].iloc[-1]
        last_sl = swings_low["Swing_Low"].iloc[-1]
        leg = last_sh - last_sl
        if leg > 0:
            pull_dist = (last_sh - close) if bias == "Bullish" else (close - last_sl)
            depth = pull_dist / leg
            if depth < 0.382:
                depth_label = "shallow"
            elif depth <= 0.618:
                depth_label = "healthy"
            else:
                depth_label = "deep"

    phase = "Range"
    confirmation = "No confirmed direction"

    if break_kind in ("CHoCH", "BOS"):
        phase = "Reversal" if break_kind == "CHoCH" else "Continuation"
        if displacement_hit and retest_held:
            confirmation = "Confirmed — displacement + retest held"
        elif displacement_hit:
            confirmation = "Displacement confirmed — awaiting retest"
        elif retest_held:
            confirmation = "Retest held, but no displacement on the break"
        else:
            confirmation = "Unconfirmed — no displacement, no retest yet"

    elif bias in ("Bullish", "Bearish"):
        if depth is not None and depth > 1.0:
            phase = "Invalidation Risk"
            confirmation = "Pullback deeper than the whole last leg — trend may be failing"
        elif depth is not None and a and pull_dist >= PULLBACK_ATR_MULT * a:
            phase = "Pullback"
            confirmation = f"Pullback in progress ({depth_label}, {depth * 100:.0f}% of last leg) — wait for it to hold"
        else:
            phase = "Trend Leg"
            confirmation = "Trend intact — waiting for the next structure break"

    else:  # Neutral
        if sweep:
            phase = "Potential Reversal"
            confirmation = (
                f"{sweep['side'].capitalize()} liquidity ({sweep['name']}) swept at {sweep['level']:,.2f}, "
                f"rejected ({sweep['dir']}) — needs a structure break to confirm"
            )
        else:
            phase = "Range"
            confirmation = "No confirmed direction — range"

    return {
        "phase": phase,
        "confirmation": confirmation,
        "depth": depth,
        "depth_label": depth_label,
        "retest_held": retest_held,
        "sweep": sweep,
    }


def invalidation_level(bias, phase, swings_high, swings_low, sweep):
    """The price that proves the current read wrong. Returns
    {"price", "side": "below"/"above"} or None."""
    if phase == "Potential Reversal" and sweep:
        side = "below" if sweep["dir"] == "bullish" else "above"
        return {"price": sweep["extreme"], "side": side}
    if bias == "Bullish" and len(swings_low):
        return {"price": float(swings_low["Swing_Low"].iloc[-1]), "side": "below"}
    if bias == "Bearish" and len(swings_high):
        return {"price": float(swings_high["Swing_High"].iloc[-1]), "side": "above"}
    return None


def build_trade_idea(a, price):
    """Informational only. Entry zone = nearest aligned order block / FVG
    on the pullback side of price; stop = beyond the invalidation level
    plus an ATR buffer; targets at 1R / 2R / 3R. Returns None if there's no
    sensible setup."""
    phase, bias = a["phase"], a["bias"]
    atr_val, inv = a["atr"], a["invalidation"]
    if phase in ("Range", "Invalidation Risk") or not atr_val or not inv:
        return None

    if phase == "Potential Reversal" and a["sweep"]:
        direction = 1 if a["sweep"]["dir"] == "bullish" else -1
    elif bias == "Bullish":
        direction = 1
    elif bias == "Bearish":
        direction = -1
    else:
        return None

    cands = []
    for z in a["zones"]:
        cands.append(("Order block", float(z["low"]), float(z["high"])))
    for f in a["fvgs"]:
        if (direction == 1 and f["kind"] == "bullish") or (direction == -1 and f["kind"] == "bearish"):
            cands.append(("FVG", f["bottom"], f["top"]))

    best = None
    for name, lo, hi in cands:
        if direction == 1:
            if lo > price or lo <= inv["price"]:
                continue
            dist = max(0.0, price - hi)
        else:
            if hi < price or hi >= inv["price"]:
                continue
            dist = max(0.0, lo - price)
        if best is None or dist < best[0]:
            best = (dist, name, lo, hi)

    if best:
        _, zone_name, zlo, zhi = best
        entry = price if zlo <= price <= zhi else (zlo + zhi) / 2
        zone = (zlo, zhi)
    else:
        zone_name, zone, entry = "Market (no aligned zone nearby)", None, price

    buffer = SL_BUFFER_ATR_MULT * atr_val
    stop = inv["price"] - buffer if direction == 1 else inv["price"] + buffer
    risk = (entry - stop) * direction
    if risk < MIN_RISK_ATR_MULT * atr_val or risk > MAX_RISK_ATR_MULT * atr_val:
        return None

    targets = [entry + direction * risk * r for r in (1, 2, 3)]

    conditional = phase in ("Reversal", "Potential Reversal") and not a["confirmation"].startswith("Confirmed")
    return {
        "direction": "LONG" if direction == 1 else "SHORT",
        "zone_name": zone_name,
        "zone": zone,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "conditional": conditional,
    }


def full_analysis(df, htf):
    """Runs everything once and returns one dict that both the chart and
    the message use, so they always agree."""
    (notes, bias, break_kind, break_index, break_level, swings_high,
     swings_low, order_block, displacement_hit, atr_val) = analyze_structure_deep(df)

    levels, key_levels = build_levels(df, swings_high, swings_low, atr_val)
    ph = classify_phase(df, bias, break_kind, break_level, swings_high, swings_low,
                        displacement_hit, atr_val, levels)

    zones = []
    if bias in ("Bullish", "Bearish"):
        swings_for_ob = swings_low if bias == "Bullish" else swings_high
        zones = find_order_blocks_multi(df, bias, swings_for_ob, lookback=OB_LOOKBACK, max_zones=3)

    price = float(df["Close"].iloc[-1])
    in_zone = any(z["low"] <= price <= z["high"] for z in zones)

    fvgs = detect_fvgs(df, atr_val)
    in_fvg = next((f for f in fvgs if f["bottom"] <= price <= f["top"]), None)
    pd_info = premium_discount(price, swings_high, swings_low)
    session_name, session_note = current_session()
    align_state, align_text = htf_alignment(bias, htf)
    inv = invalidation_level(bias, ph["phase"], swings_high, swings_low, ph["sweep"])

    extra_notes = list(notes)
    if ph["sweep"] and ph["phase"] != "Potential Reversal":
        s = ph["sweep"]
        extra_notes.append(
            f"{s['side'].capitalize()} liquidity ({s['name']}) swept at {s['level']:,.2f} ({s['dir']} rejection)"
        )
    if in_zone:
        extra_notes.append("Price is inside an order-block zone")
    if in_fvg:
        extra_notes.append(f"Price is inside a {in_fvg['kind']} FVG ({in_fvg['bottom']:,.2f} – {in_fvg['top']:,.2f})")

    a = {
        "notes": extra_notes,
        "bias": bias,
        "break_kind": break_kind,
        "break_index": break_index,
        "break_level": break_level,
        "swings_high": swings_high,
        "swings_low": swings_low,
        "order_block": order_block,
        "zones": zones,
        "in_zone": in_zone,
        "fvgs": fvgs,
        "in_fvg": in_fvg,
        "pd": pd_info,
        "key_levels": key_levels,
        "session": session_name,
        "session_note": session_note,
        "htf": htf,
        "align_state": align_state,
        "align_text": align_text,
        "invalidation": inv,
        "displacement_hit": displacement_hit,
        "atr": atr_val,
        "price": price,
        **ph,
    }
    a["trade"] = build_trade_idea(a, price)
    return a


# ================== CHART ==================
def create_chart(df, a, filename="xauusd_chart.png"):
    bias = a["bias"]
    phase = a["phase"]
    atr_val = a["atr"]
    swings_high, swings_low = a["swings_high"], a["swings_low"]
    break_kind, break_index, break_level = a["break_kind"], a["break_index"], a["break_level"]
    htf = a["htf"]

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit", wick="inherit")
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=":",
        gridcolor="#b0bec5",
        facecolor="#e3f2fd",
        figcolor="#e3f2fd",
        y_on_right=True,
    )

    htf_txt = f" | {htf['interval']} {htf['bias']}" if htf else ""
    plot_df = df.tail(80)
    fig, axes = mpf.plot(
        plot_df,
        type="candle",
        style=style,
        title=f"XAUUSD {INTERVAL.upper()} | {bias} | {phase}{htf_txt} | {a['session']}",
        ylabel="Price",
        volume=False,
        figsize=(13, 7),
        returnfig=True,
    )
    ax = axes[0]
    offset_start = len(df) - len(plot_df)
    last_x = len(plot_df) - 1
    a_ = atr_val or 1
    view_lo = plot_df["Low"].min() - 3 * a_
    view_hi = plot_df["High"].max() + 3 * a_

    def in_view(p):
        return view_lo <= p <= view_hi

    # --- key levels (PDH/PDL, Asia, EQH/EQL): labels sit just ABOVE their line ---
    for lv in a["key_levels"]:
        if not in_view(lv["price"]):
            continue
        ax.axhline(lv["price"], linestyle="-.", linewidth=0.8, color="#455a64", alpha=0.7, zorder=1)
        ax.text(0.005, lv["price"], lv["name"], transform=ax.get_yaxis_transform(),
                fontsize=7, fontweight="bold", color="#37474f", va="bottom", ha="left")

    # --- premium / discount equilibrium line: label sits just BELOW its line
    # so it can't collide with a key-level label at a nearby price ---
    pdi = a["pd"]
    if pdi and in_view(pdi["eq"]):
        ax.axhline(pdi["eq"], linestyle=":", linewidth=1.0, color="#8e24aa", alpha=0.8, zorder=1)
        ax.text(0.005, pdi["eq"], "EQ 50%", transform=ax.get_yaxis_transform(),
                fontsize=7, fontweight="bold", color="#8e24aa", va="top", ha="left")

    # --- fair value gaps ---
    for f in a["fvgs"]:
        x0 = max(f["pos"] - offset_start, 0)
        col = "#1e88e5" if f["kind"] == "bullish" else "#fb8c00"
        rect = plt.Rectangle((x0, f["bottom"]), last_x - x0, f["top"] - f["bottom"],
                              facecolor=col + "26", edgecolor=col, linewidth=0.6, linestyle="--", zorder=1)
        ax.add_patch(rect)
        ax.annotate("FVG", xy=(x0 + 0.5, f["top"]), fontsize=7, fontweight="bold", color=col, va="bottom")

    # --- swing markers, each labeled against the swing before it ---
    sh_vals = swings_high["Swing_High"]
    for i in range(max(0, len(sh_vals) - 2), len(sh_vals)):
        idx, val = sh_vals.index[i], sh_vals.iloc[i]
        pos = df.index.get_loc(idx) - offset_start
        if pos < 0:
            continue
        label = ("Lower High" if val < sh_vals.iloc[i - 1] else "Higher High") if i >= 1 else "Swing High"
        ax.annotate("", xy=(pos, val), xytext=(pos, val + a_ * 1.8),
                    arrowprops=dict(arrowstyle="-|>", color="#d32f2f", lw=2))
        ax.annotate(label, xy=(pos, val + a_ * 2.0),
                    ha="center", fontsize=8, fontweight="bold", color="#d32f2f")

    sl_vals = swings_low["Swing_Low"]
    for i in range(max(0, len(sl_vals) - 2), len(sl_vals)):
        idx, val = sl_vals.index[i], sl_vals.iloc[i]
        pos = df.index.get_loc(idx) - offset_start
        if pos < 0:
            continue
        label = ("Higher Low" if val > sl_vals.iloc[i - 1] else "Lower Low") if i >= 1 else "Swing Low"
        ax.annotate("", xy=(pos, val), xytext=(pos, val - a_ * 1.8),
                    arrowprops=dict(arrowstyle="-|>", color="#2e7d32", lw=2))
        ax.annotate(label, xy=(pos, val - a_ * 2.0),
                    ha="center", fontsize=8, fontweight="bold", color="#2e7d32")

    # --- BOS/CHoCH line ---
    if break_kind and break_index is not None:
        bx = break_index - offset_start
        if bx >= 0:
            col = "#d500f9" if break_kind == "CHoCH" else "#111"
            ax.plot([bx, last_x], [break_level, break_level], linestyle="--", linewidth=1, color=col, alpha=0.8)
            tag = break_kind + (" ✔ retest held" if a["retest_held"] else "")
            ax.annotate(tag, xy=((bx + last_x) / 2, break_level), fontsize=9, fontweight="bold",
                        color=col, ha="center", va="bottom")

    # --- liquidity sweep marker ---
    sweep = a["sweep"]
    if sweep:
        sx = sweep["pos"] - offset_start
        if sx >= 0:
            ax.plot([max(sx - 6, 0), last_x], [sweep["level"], sweep["level"]],
                    linestyle=":", linewidth=1.2, color="#6a1b9a")
            ax.annotate(f"Sweep ({sweep['name']})", xy=(sx, sweep["level"]), fontsize=8, fontweight="bold",
                        color="#6a1b9a", ha="center",
                        va="bottom" if sweep["side"] == "buy-side" else "top")

    # --- order-block zones or range box ---
    zones = a["zones"]
    if bias in ("Bullish", "Bearish"):
        zone_color = "#26a69a" if bias == "Bullish" else "#ef5350"
        for i, ob in enumerate(zones):
            rect = plt.Rectangle((0, ob["low"]), last_x, ob["high"] - ob["low"],
                                  facecolor=zone_color + "22", edgecolor=zone_color, linewidth=0.8, zorder=1)
            ax.add_patch(rect)
            if i == 0:
                ax.annotate("Order Block / Re-Sweep Zone", xy=(last_x * 0.15, (ob["high"] + ob["low"]) / 2),
                            color=zone_color, fontsize=8, fontweight="bold",
                            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor=zone_color, alpha=0.9))
    elif len(swings_high) >= 1 and len(swings_low) >= 1:
        range_hi = swings_high["Swing_High"].iloc[-1]
        range_lo = swings_low["Swing_Low"].iloc[-1]
        rect = plt.Rectangle((0, range_lo), last_x, range_hi - range_lo,
                              facecolor="#9e9e9e22", edgecolor="#616161", linewidth=0.8, zorder=1)
        ax.add_patch(rect)
        ax.annotate("Range — awaiting breakout", xy=(last_x * 0.15, (range_hi + range_lo) / 2),
                    color="#616161", fontsize=8, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#616161", alpha=0.9))

    # --- invalidation line ---
    inv = a["invalidation"]
    if inv and in_view(inv["price"]):
        ax.axhline(inv["price"], linestyle=":", linewidth=1.3, color="#b71c1c", zorder=2)
        ax.text(0.995, inv["price"], f"Invalidation ({inv['side']}) {inv['price']:,.2f}",
                transform=ax.get_yaxis_transform(), fontsize=7, fontweight="bold",
                color="#b71c1c", va="bottom", ha="right")

    # --- phase badge: sits just ABOVE the plot area (outside it), so it never
    # covers level labels or candles ---
    phase_color = PHASE_COLORS.get(phase, "#616161")
    ax.text(0.0, 1.012, f"PHASE: {phase.upper()}", transform=ax.transAxes, fontsize=10,
            fontweight="bold", color="white", va="bottom", ha="left",
            bbox=dict(boxstyle="round,pad=0.4", facecolor=phase_color, edgecolor="none"))

    # --- current price badge ---
    last_price = df["Close"].iloc[-1]
    badge_color = "#26a69a" if bias == "Bullish" else ("#ef5350" if bias == "Bearish" else "#616161")
    ax.annotate(f"{last_price:,.2f}", xy=(last_x, last_price), xytext=(12, 0), textcoords="offset points",
                fontsize=10, fontweight="bold", color="white", va="center",
                bbox=dict(boxstyle="round,pad=0.4", facecolor=badge_color, edgecolor="none"))

    # --- ATR-based projected target (directional bias only) ---
    if atr_val and bias in ("Bullish", "Bearish"):
        direction = 1 if bias == "Bullish" else -1
        target = last_price + direction * PROJECTION_ATR_MULT * atr_val
        mid_x = last_x + (len(plot_df) * 0.15)
        end_x = last_x + (len(plot_df) * 0.3)
        pullback = last_price - direction * atr_val * 0.5
        ax.plot([last_x, mid_x, end_x], [last_price, pullback, target],
                linestyle="--", linewidth=1.2, color="#616161", alpha=0.8)
        ax.annotate("Price range", xy=(end_x, target), xytext=(6, 0), textcoords="offset points",
                    fontsize=8, fontweight="bold", color="#424242")
        ax.set_xlim(right=end_x + 3)

    plt.savefig(filename, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return filename


# ================== MESSAGE ==================
def outlook_line(phase, bias):
    d = "bullish" if bias == "Bullish" else "bearish"
    opp = "bearish" if bias == "Bullish" else "bullish"
    lines = {
        "Continuation": f"Trend continuation ({d}). Look for entries on a pullback to the zone or the broken level.",
        "Reversal": f"Possible reversal to {d}. Wait for a retest of the broken level to hold before trusting it.",
        "Pullback": f"Pullback inside a {d} trend. Wait for a reaction at the zone, then a resumption {d}.",
        "Trend Leg": f"{d.capitalize()} leg in progress. No new break yet, so wait for the next structure break or a pullback.",
        "Invalidation Risk": f"Pullback has exceeded the last {d} leg. The trend is at risk of flipping {opp}.",
        "Potential Reversal": "Liquidity was taken and rejected. Wait for a break of structure to confirm direction.",
        "Range": "No trade bias until price breaks the range.",
    }
    return lines.get(phase, "Wait for reaction at the zone.")


def create_caption(a, price):
    """Short caption for the photo (Telegram caps captions at 1024 chars)."""
    htf = a["htf"]
    htf_part = f" | {htf['interval']}: {htf['bias']}" if htf else ""
    return (
        f"📌 <b>XAUUSD {INTERVAL.upper()} — {a['phase']}</b>\n"
        f"Price <b>{price:,.2f}</b> | Bias: <b>{a['bias']}</b>{htf_part}\n"
        f"{a['confirmation']}"
    )[:1000]


def create_message(a, price):
    date_str = now_utc().strftime("%B %d").upper()
    bias, phase = a["bias"], a["phase"]
    swings_high, swings_low = a["swings_high"], a["swings_low"]
    notes_text = "\n".join(f"• {n}" for n in a["notes"]) if a["notes"] else "• Structure developing"

    parts = []
    parts.append(
        f"📌 <b>MARKET UPDATE – {date_str}</b>\n"
        f"— {PAIR.replace('/', '')} / {INTERVAL.upper()} —\n\n"
        f"XAUUSD is trading around <b>{price:,.2f}</b>.\n"
        f"🕒 Session: <b>{a['session']}</b> — {a['session_note']}\n\n"
        f"<b>Phase:</b> {phase}\n"
        f"<b>Status:</b> {a['confirmation']}\n"
        f"<b>HTF:</b> {a['align_text']}\n"
    )
    parts.append(f"<b>Structure:</b>\n{notes_text}\n")

    # premium / discount
    pdi = a["pd"]
    if pdi:
        parts.append(
            f"<b>Location:</b> {pdi['label']} ({pdi['pct'] * 100:.0f}% of range "
            f"{pdi['lo']:,.2f} – {pdi['hi']:,.2f}, EQ {pdi['eq']:,.2f})\n"
        )

    # key levels
    if a["key_levels"]:
        kl = " | ".join(f"{k['name']} {k['price']:,.2f}" for k in a["key_levels"])
        parts.append(f"<b>Key levels:</b> {kl}\n")

    # FVGs
    if a["fvgs"]:
        lines = []
        for f in a["fvgs"][::-1]:
            fill = f" ({f['filled'] * 100:.0f}% filled)" if f["filled"] > 0 else ""
            lines.append(f"• {f['kind'].capitalize()} FVG {f['bottom']:,.2f} – {f['top']:,.2f}{fill}")
        parts.append("<b>Fair value gaps:</b>\n" + "\n".join(lines) + "\n")

    # order block (nearest zone to price) or range
    zones = a["zones"]
    if bias in ("Bullish", "Bearish"):
        if a["in_zone"]:
            parts.append("Price is reacting inside an order-block zone.\n")
        elif zones:
            z = min(zones, key=lambda zz: min(abs(price - zz["low"]), abs(price - zz["high"])))
            parts.append(f"Nearest order-block zone: {z['low']:,.2f} – {z['high']:,.2f}.\n")
    elif len(swings_high) >= 1 and len(swings_low) >= 1:
        range_hi = swings_high["Swing_High"].iloc[-1]
        range_lo = swings_low["Swing_Low"].iloc[-1]
        parts.append(
            f"Price is between <b>{range_lo:,.2f}</b> and <b>{range_hi:,.2f}</b>.\n"
            f"Watch a break above <b>{range_hi:,.2f}</b> (bullish) or below <b>{range_lo:,.2f}</b> (bearish).\n"
        )

    # invalidation
    inv = a["invalidation"]
    if inv:
        parts.append(
            f"⚠️ <b>Invalidation:</b> a close {inv['side']} <b>{inv['price']:,.2f}</b> "
            f"breaks this read.\n"
        )

    # trade idea
    t = a["trade"]
    if t:
        zone_txt = f"{t['zone_name']} {t['zone'][0]:,.2f} – {t['zone'][1]:,.2f}" if t["zone"] else t["zone_name"]
        tp = " | ".join(f"TP{i + 1} {v:,.2f}" for i, v in enumerate(t["targets"]))
        cond = "Conditional: wait for confirmation first." if t["conditional"] else "Look for a reaction at the zone."
        counter = " Counter-HTF, so size down or skip." if a["align_state"] == "counter" else ""
        parts.append(
            f"\n🎯 <b>TRADE IDEA</b> (informational, not a signal)\n"
            f"<b>{t['direction']}</b> — {zone_txt}\n"
            f"Entry ~{t['entry']:,.2f} | Stop {t['stop']:,.2f}\n"
            f"{tp} (1R / 2R / 3R)\n"
            f"{cond}{counter}\n"
        )

    # projection target
    if a["atr"] and bias in ("Bullish", "Bearish"):
        direction = 1 if bias == "Bullish" else -1
        target = price + direction * PROJECTION_ATR_MULT * a["atr"]
        parts.append(f"\nProjected range target (ATR-based): <b>{target:,.2f}</b>\n")

    bias_txt = f"<b>Bias:</b> {bias} — {outlook_line(phase, bias)}"
    parts.append("\n" + bias_txt + "\n")

    msg = "\n".join(p.rstrip("\n") for p in parts)
    if len(msg) > 4000:  # Telegram message limit is 4096; cut at a line boundary
        msg = msg[:4000].rsplit("\n", 1)[0]
    return msg


# ================== REPEAT SKIPPING ==================
def make_signature(a):
    htf_bias = a["htf"]["bias"] if a["htf"] else "NA"
    sweep_name = a["sweep"]["name"] if a["sweep"] else "none"
    return "|".join([
        a["phase"], a["bias"], str(a["break_kind"]), htf_bias,
        f"zone={int(a['in_zone'])}", f"fvg={int(a['in_fvg'] is not None)}", f"sweep={sweep_name}",
    ])


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(signature):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump({"signature": signature, "last_post_utc": now_utc().isoformat()}, f)
    except Exception as e:
        print("Could not save state:", e)


def should_post(signature):
    if not SKIP_REPEATS:
        return True, "repeat-skipping is off"
    st = load_state()
    if st.get("signature") != signature:
        return True, "setup changed"
    last = st.get("last_post_utc")
    if not last:
        return True, "no previous post time"
    if FORCE_POST_HOURS > 0:
        try:
            age_h = (now_utc() - datetime.fromisoformat(last)).total_seconds() / 3600
            if age_h >= FORCE_POST_HOURS:
                return True, f"heartbeat ({age_h:.1f}h since last post)"
        except Exception:
            return True, "could not read last post time"
    return False, "same setup as the last post"


# ================== SEND ==================
def _telegram_post(method, data, files=None):
    import requests
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    last = None
    for attempt in range(1, 4):
        try:
            resp = requests.post(url, data=data, files=files, timeout=30)
            last = resp.json()
            if last.get("ok"):
                return last
        except Exception as e:
            last = {"ok": False, "error": str(e)}
        if attempt < 3:
            time.sleep(2 * attempt)
            if files:  # rewind the file so the retry re-sends the full image
                for f in files.values():
                    f.seek(0)
    return last


def send_telegram_update(photo_path, caption, message):
    with open(photo_path, "rb") as photo:
        r1 = _telegram_post("sendPhoto",
                            {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                            files={"photo": photo})
    if not r1.get("ok"):
        return r1
    return _telegram_post("sendMessage",
                          {"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML",
                           "disable_web_page_preview": True})


# ================== MAIN ==================
def main():
    if not is_forex_market_open():
        print("Forex market is closed (weekend) — skipping this run to avoid posting a stale-data update.")
        return

    try:
        df, price = get_live_data()

        fresh, age_min = check_fresh(df)
        if not fresh:
            print(f"Last candle is {age_min:.0f} min old (limit {STALE_BARS * interval_minutes(INTERVAL)} min) — "
                  "data looks stale, skipping this run.")
            return

        htf = get_htf_context()
        analysis = full_analysis(df, htf)

        signature = make_signature(analysis)
        go, reason = should_post(signature)
        if not go:
            print(f"Skipping post: {reason} [{signature}]")
            return

        chart_file = create_chart(df, analysis)
        caption = create_caption(analysis, price)
        message = create_message(analysis, price)
        result = send_telegram_update(chart_file, caption, message)

        if result.get("ok"):
            save_state(signature)
            print(f"Update sent ({reason}) — phase: {analysis['phase']} / bias: {analysis['bias']}")
        else:
            print("Error:", result)

        if os.path.exists(chart_file):
            os.remove(chart_file)

    except Exception as e:
        print("Error:", str(e))


if __name__ == "__main__":
    main()
