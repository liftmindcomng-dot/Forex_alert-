"""
Standalone XAUUSD Market Update poster.

Separate from forex_alert.py by design. Posts a narrative structure chart
to Telegram. Skips weekends (same guard as forex_alert.py).

Feed: Twelve Data XAU/USD spot (TWELVE_DATA_API_KEY).

What it does each run:
  1. Fetches the main timeframe (default 30min) AND a higher timeframe
     (default 4h) with retries, and skips the post if the last candle is stale.
  2. Structure: swings, BOS vs CHoCH (one shared scan for chart AND message),
     displacement, order blocks.
  3. Market phase: Continuation / Reversal / Pullback / Trend Leg /
     Invalidation Risk / Potential Reversal / Range, with a confirmation
     status (displacement + retest).
  4. HTF bias and whether the current read is aligned with it or counter-trend.
  5. Fair value gaps, premium/discount (50% line), key levels (previous day
     high/low, Asia range, equal highs/lows) and liquidity sweeps.
  6. Session tag, invalidation level, and an informational trade idea.
  7. Projection: liquidity re-sweep zone + two "price range" targets.
  8. Skips near-identical repeat posts (state file), with a heartbeat post.
  9. Position guidance from open_trade.json (open trade or last stop-out):
     Fresh entry / Re-entry / Wait / Don't enter, or Hold / Move to breakeven /
     Exit / Stop reached. Informational only.

Chart style: TradingView-like SMC layout drawn with plain matplotlib
(mplfinance is no longer needed): cyan gradient background, structure box,
BOS/CHoCH lines along the whole visible history, impulse-leg boxes, supply /
demand bands, arrow swing labels, re-sweep zone callouts, black projection
path, price-range lines, price badges and a volume profile on the right.
"""

import os
import sys
import json
import time
import urllib.request
import urllib.parse
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
from matplotlib.colors import LinearSegmentedColormap
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
BREAK_FRESH_BARS = int(os.getenv("BREAK_FRESH_BARS", "5"))       # a structure break counts as "current" for this many bars
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
FVG_MIN_ATR_MULT = float(os.getenv("FVG_MIN_ATR_MULT", "0.3"))
FVG_LOOKBACK = int(os.getenv("FVG_LOOKBACK", "60"))
FVG_MAX_ZONES = int(os.getenv("FVG_MAX_ZONES", "3"))
EQ_TOL_ATR_MULT = float(os.getenv("EQ_TOL_ATR_MULT", "0.15"))
ASIA_START_HOUR = int(os.getenv("ASIA_START_HOUR", "0"))         # UTC
ASIA_END_HOUR = int(os.getenv("ASIA_END_HOUR", "7"))             # UTC

# trade idea
SL_BUFFER_ATR_MULT = float(os.getenv("SL_BUFFER_ATR_MULT", "0.3"))
MIN_RISK_ATR_MULT = float(os.getenv("MIN_RISK_ATR_MULT", "0.2"))
MAX_RISK_ATR_MULT = float(os.getenv("MAX_RISK_ATR_MULT", "5.0"))

# chart
CHART_BARS = int(os.getenv("CHART_BARS", "80"))
IMPULSE_ATR_MULT = float(os.getenv("IMPULSE_ATR_MULT", "4.0"))   # min size of an "impulse leg" box
IMPULSE_MAX_BARS = int(os.getenv("IMPULSE_MAX_BARS", "14"))
PROFILE_BINS = int(os.getenv("PROFILE_BINS", "40"))
MAX_TARGET_ATR = float(os.getenv("MAX_TARGET_ATR", "8"))         # ignore "price range" levels farther than this

# repeat-skipping
SKIP_REPEATS = os.getenv("SKIP_REPEATS", "true").lower() == "true"
FORCE_POST_HOURS = float(os.getenv("FORCE_POST_HOURS", "4"))     # 0 = never force a heartbeat post
STATE_FILE = os.getenv("XAU_STATE_FILE", "state_xau_update.json")
OPEN_TRADE_FILE = os.getenv("XAU_OPEN_TRADE_FILE", "open_trade.json")   # written by you or the EA

# data safety
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "3"))
STALE_BARS = int(os.getenv("STALE_BARS", "3"))

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
    """Fetch candles with retries. Raises RuntimeError if every attempt fails.
    Volume is included when the feed provides it (otherwise 0)."""
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
        "Volume": [float(r.get("volume") or 0) for r in rows],
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
    """Last opposite-colored candle before the impulsive breakout leg.
    Returns {"high","low","pos"} (pos = bar index of the candle) or None."""
    opens, closes = df["Open"].values, df["Close"].values
    highs, lows = df["High"].values, df["Low"].values
    start = max(0, before_idx - lookback)
    for i in range(before_idx - 1, start - 1, -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]
        if (bias == "Bullish" and is_bearish) or (bias == "Bearish" and is_bullish):
            return {"high": highs[i], "low": lows[i], "pos": i}
    return None


def find_order_blocks_multi(df, bias, swings, lookback=OB_LOOKBACK, max_zones=3):
    zones = []
    for idx in swings.tail(max_zones).index:
        before_idx = df.index.get_loc(idx)
        ob = find_order_block(df, bias, before_idx=before_idx, lookback=lookback)
        if ob and ob not in zones:
            zones.append(ob)
    return zones


def analyze_structure_deep(df, use_events=True):
    """Swing bias needs the last-two swing highs AND last-two swing lows to
    agree (both lower = Bearish, both higher = Bullish); disagreement =
    Neutral range.

    Breaks of structure come from detect_structure_events(), the same scan
    the chart draws, so the message and the chart always agree. A break is
    "current" when it happened within BREAK_FRESH_BARS bars and price is
    still beyond the broken level. A current break sets the bias to its
    direction (a CHoCH flips it, a BOS keeps it). Displacement: the breaking
    candle's range >= DISPLACEMENT_ATR_MULT x ATR. Pass use_events=False for
    a pure swing read (used for the higher timeframe).

    Returns (structure_notes, bias, break_kind, break_index, break_level,
    swings_high, swings_low, order_block, displacement_hit, atr_val,
    break_pos). break_index = bar of the broken swing, break_pos = bar that
    broke it.
    """
    raw = df
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

    break_kind = None
    break_index = None
    break_level = None
    break_pos = None
    order_block = None
    displacement_hit = False

    if use_events:
        n = len(raw)
        last_close = float(raw["Close"].iloc[-1])
        fresh = [e for e in detect_structure_events(raw)
                 if e["kind"] and e["to_pos"] >= n - BREAK_FRESH_BARS
                 and ((e["dir"] == "bull" and last_close > e["level"])
                      or (e["dir"] == "bear" and last_close < e["level"]))]
        if fresh:
            e = fresh[-1]
            bias = "Bullish" if e["dir"] == "bull" else "Bearish"
            break_kind, break_level = e["kind"], e["level"]
            break_index, break_pos = e["from_pos"], e["to_pos"]
            brk_range = float(raw["High"].iloc[break_pos] - raw["Low"].iloc[break_pos])
            displacement_hit = bool(atr_val) and brk_range >= DISPLACEMENT_ATR_MULT * atr_val
            order_block = find_order_block(raw, bias, before_idx=break_pos, lookback=OB_LOOKBACK)
            structure_notes.append(
                f"{break_kind} ({bias.lower()}) confirmed"
                + (" with displacement" if displacement_hit else " (no displacement)")
            )

    return (structure_notes, bias, break_kind, break_index, break_level,
            swings_high, swings_low, order_block, displacement_hit, atr_val, break_pos)


# ================== HIGHER TIMEFRAME ==================
def get_htf_context():
    """Higher-timeframe bias. Returns None if the HTF fetch fails (the
    post still goes out, just without the HTF line)."""
    try:
        htf_df = fetch_candles(PAIR, HTF_INTERVAL, HTF_OUTPUTSIZE)
        res = analyze_structure_deep(htf_df, use_events=False)
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


# ================== STRUCTURE EVENTS / IMPULSE LEGS ==================
def detect_structure_events(df, left=SWING_LOOKBACK, right=SWING_LOOKBACK):
    """Walks the whole window bar by bar and records every structure break:
    a close beyond the latest confirmed, not-yet-broken swing. A break in the
    direction of the current trend is a BOS, a break against it is a CHoCH.
    The very first break (no trend known yet) is recorded with kind None and
    isn't labeled. Returns a list of dicts: kind, dir ("bull"/"bear"),
    level, from_pos, to_pos. Used by BOTH the analysis (message) and the chart."""
    d = detect_swings(df, left, right)
    highs = d["Swing_High"].values
    lows = d["Swing_Low"].values
    closes = df["Close"].values
    n = len(df)
    events = []
    trend = None
    cur_high = None
    cur_low = None
    for j in range(n):
        k = j - right  # the swing at bar k is only confirmed once bar j has closed
        if k >= 0:
            if not np.isnan(highs[k]):
                cur_high = (k, float(highs[k]))
            if not np.isnan(lows[k]):
                cur_low = (k, float(lows[k]))
        if cur_high is not None and closes[j] > cur_high[1]:
            kind = "BOS" if trend == "bull" else ("CHoCH" if trend == "bear" else None)
            events.append({"kind": kind, "dir": "bull", "level": cur_high[1],
                           "from_pos": cur_high[0], "to_pos": j})
            trend = "bull"
            cur_high = None
        elif cur_low is not None and closes[j] < cur_low[1]:
            kind = "BOS" if trend == "bear" else ("CHoCH" if trend == "bull" else None)
            events.append({"kind": kind, "dir": "bear", "level": cur_low[1],
                           "from_pos": cur_low[0], "to_pos": j})
            trend = "bear"
            cur_low = None
    return events


def find_impulse_legs(df, atr_val, max_legs=2):
    """Big, fast swing-to-swing moves (>= IMPULSE_ATR_MULT x ATR within
    IMPULSE_MAX_BARS bars). Returns the most recent few."""
    if not atr_val:
        return []
    d = detect_swings(df)
    pts = []
    for i in range(len(d)):
        h = d["Swing_High"].iloc[i]
        l = d["Swing_Low"].iloc[i]
        if not np.isnan(h):
            pts.append((i, float(h), "H"))
        if not np.isnan(l):
            pts.append((i, float(l), "L"))
    pts.sort(key=lambda p: p[0])
    legs = []
    for (i0, p0, t0), (i1, p1, t1) in zip(pts, pts[1:]):
        if t0 == t1 or i1 == i0:
            continue
        if abs(p1 - p0) >= IMPULSE_ATR_MULT * atr_val and (i1 - i0) 
