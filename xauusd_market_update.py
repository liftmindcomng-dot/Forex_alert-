"""
Standalone XAUUSD Market Update poster.

Separate from forex_alert.py by design. Posts a narrative structure chart
to Telegram. Skips weekends and optional skip dates (same guard as
forex_alert.py).

Feed: Twelve Data XAU/USD spot (TWELVE_DATA_API_KEY).

What it does each run:
  1. Fetches the main timeframe (default 30min) AND a higher timeframe
     (default 4h) with retries, and skips the post if the last candle is stale.
  2. Structure: swings, BOS vs CHoCH (one shared scan for chart AND message),
     displacement, order blocks.
  3. Market phase: Continuation / Reversal / Pullback / Trend Leg /
     Invalidation Risk / Potential Reversal / Range / Breakout / Confirmed
     Breakout, with a confirmed flag on every phase.
  4. HTF bias and whether the current read is aligned with it or counter-trend.
  5. Fair value gaps, premium/discount (50% line), key levels (previous day
     high/low, Asia range, equal highs/lows) and liquidity sweeps.
  6. Session tag, invalidation level, and a watch level. SMC reads use an
     order block / FVG level; range breakouts use the broken edge. The bot
     never places orders; you place them manually.
  7. Projection: liquidity re-sweep zone + two "price range" targets.
  8. Skips near-identical repeat posts (state file), with a heartbeat post.
  9. Position guidance from open_trade.json (open trade or last stop-out):
     Setup at level / Re-entry / Wait / Don't enter, or Hold / Caution /
     Move to breakeven / Exit / Stop reached / Check trade file.
     Informational only. You update open_trade.json by hand after each
     manual trade action.

Chart style: TradingView-like SMC layout drawn with plain matplotlib
(mplfinance is no longer needed): cyan gradient background, structure box,
BOS/CHoCH lines along the whole visible history, impulse-leg boxes, supply /
demand bands, arrow swing labels, re-sweep zone callouts, black projection
path, price-range lines, price badges, range box for breakouts, and a volume
profile on the right.
"""

import os
import json
import time
import urllib.request
import urllib.parse
import urllib.error
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
INTERVAL = os.getenv("XAU_INTERVAL", "15min")
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

# range breakout
RANGE_BARS = int(os.getenv("RANGE_BARS", "20"))
RANGE_MAX_ATR_MULT = float(os.getenv("RANGE_MAX_ATR_MULT", "2.5"))        # max range width, in ATR
BREAK_BUFFER_ATR_MULT = float(os.getenv("BREAK_BUFFER_ATR_MULT", "0.1"))  # close must clear the edge by this much
BREAKOUT_LOOKBACK = int(os.getenv("BREAKOUT_LOOKBACK", "10"))             # how many recent bars can hold the breakout
BREAKOUT_STOP_MODE = os.getenv("BREAKOUT_STOP_MODE", "edge")              # "edge" = opposite range edge, "mid" = range midpoint

# FVG / levels
FVG_MIN_ATR_MULT = float(os.getenv("FVG_MIN_ATR_MULT", "0.3"))
FVG_LOOKBACK = int(os.getenv("FVG_LOOKBACK", "60"))
FVG_MAX_ZONES = int(os.getenv("FVG_MAX_ZONES", "3"))
EQ_TOL_ATR_MULT = float(os.getenv("EQ_TOL_ATR_MULT", "0.15"))
ASIA_START_HOUR = int(os.getenv("ASIA_START_HOUR", "0"))         # UTC
ASIA_END_HOUR = int(os.getenv("ASIA_END_HOUR", "7"))             # UTC

# trade idea (watch level only; orders are placed manually)
SL_BUFFER_ATR_MULT = float(os.getenv("SL_BUFFER_ATR_MULT", "0.3"))
MIN_RISK_ATR_MULT = float(os.getenv("MIN_RISK_ATR_MULT", "0.2"))
MAX_RISK_ATR_MULT = float(os.getenv("MAX_RISK_ATR_MULT", "5.0"))

# chart
CHART_BARS = int(os.getenv("CHART_BARS", "80"))
IMPULSE_ATR_MULT = float(os.getenv("IMPULSE_ATR_MULT", "4.0"))   # min size of an "impulse leg" box
IMPULSE_MAX_BARS = int(os.getenv("IMPULSE_MAX_BARS", "14"))
PROFILE_BINS = int(os.getenv("PROFILE_BINS", "40"))
MAX_TARGET_ATR = float(os.getenv("MAX_TARGET_ATR", "8"))         # ignore "price range" levels farther than this
CHART_FILE = "xauusd_chart.png"

# repeat-skipping
SKIP_REPEATS = os.getenv("SKIP_REPEATS", "true").lower() == "true"
FORCE_POST_HOURS = float(os.getenv("FORCE_POST_HOURS", "4"))     # 0 = never force a heartbeat post
STATE_FILE = os.getenv("XAU_STATE_FILE", "state_xau_update.json")
OPEN_TRADE_FILE = os.getenv("XAU_OPEN_TRADE_FILE", "open_trade.json")   # updated by hand after each manual trade action
OPEN_TRADE_MAX_AGE_HOURS = float(os.getenv("OPEN_TRADE_MAX_AGE_HOURS", "12"))

# data safety
FETCH_RETRIES = int(os.getenv("FETCH_RETRIES", "3"))
RETRY_DELAY = float(os.getenv("RETRY_DELAY", "3"))
RATE_LIMIT_WAIT = float(os.getenv("RATE_LIMIT_WAIT", "60"))      # Twelve Data credit windows are per minute
STALE_BARS = int(os.getenv("STALE_BARS", "3"))
# Days to skip entirely (UTC, YYYY-MM-DD, comma-separated), e.g. "2026-12-25,2027-01-01"
SKIP_DATES = {d.strip() for d in os.getenv("XAU_SKIP_DATES", "").split(",") if d.strip()}

TELEGRAM_CHUNK = 4000   # Telegram message limit is 4096

PHASE_COLORS = {
    "Continuation": "#2e7d32",
    "Reversal": "#d500f9",
    "Pullback": "#f57c00",
    "Trend Leg": "#1565c0",
    "Invalidation Risk": "#b71c1c",
    "Potential Reversal": "#6a1b9a",
    "Range": "#616161",
    "Breakout": "#00838f",
    "Confirmed Breakout": "#00695c",
}

# Phases where "CONFIRMED" in the header means a trade read has been confirmed.
# Trend Leg and Range are excluded: a confirmed trend leg is only continuation,
# not a setup, so it should not look like a signal.
CONFIRMABLE_PHASES = {"Pullback", "Continuation", "Reversal", "Invalidation Risk", "Breakout"}


# ================== HELPERS ==================
def scrub(text):
    """Remove secrets (API key, bot token) from any text before printing it."""
    s = str(text)
    for secret in (TWELVE_DATA_API_KEY, BOT_TOKEN):
        if secret:
            s = s.replace(secret, "***")
    return s


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
    Sunday until 22:00 UTC, and closed on any date listed in XAU_SKIP_DATES.
    Same weekend rule as forex_alert.py."""
    now = now_utc()
    if now.strftime("%Y-%m-%d") in SKIP_DATES:
        return False
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
def _is_rate_limit(msg):
    m = str(msg).lower()
    return "limit" in m or "credit" in m or "429" in m


def fetch_candles(pair, interval, outputsize):
    """Fetch candles with retries. Raises RuntimeError if every attempt fails.
    Rate-limit responses wait RATE_LIMIT_WAIT seconds instead of the short
    backoff, since the quota resets per minute. Volume is included when the
    feed provides it (otherwise 0)."""
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
        wait = RETRY_DELAY * attempt
        try:
            with urllib.request.urlopen(url, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
            if payload.get("values"):
                data = payload
                break
            msg = payload.get("message", payload)
            last_err = RuntimeError(scrub(msg))
            if _is_rate_limit(msg):
                wait = RATE_LIMIT_WAIT
        except urllib.error.HTTPError as e:
            last_err = RuntimeError(f"HTTP {e.code}")
            if e.code == 429:
                wait = RATE_LIMIT_WAIT
        except Exception as e:
            last_err = RuntimeError(scrub(e))
        if attempt < FETCH_RETRIES:
            time.sleep(wait)

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
    """Returns the candles and the unrounded last close. Rounding happens only
    when displaying, so the analysis and the message use the same price."""
    df = fetch_candles(pair, interval, outputsize)
    return df, float(df["Close"].iloc[-1])


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
    """A swing high is a bar whose high equals the max of the window of
    `left` bars before it and `right` bars after it (same rule as before,
    vectorized). Requires left == right so the centered window lines up."""
    df = df.copy()
    w = left + right + 1
    hi_roll = df["High"].rolling(w, center=True).max()
    lo_roll = df["Low"].rolling(w, center=True).min()
    df["Swing_High"] = df["High"].where(df["High"] == hi_roll)
    df["Swing_Low"] = df["Low"].where(df["Low"] == lo_roll)
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


# ================== RANGE BREAKOUT ==================
def detect_range_breakout(df, atr_val):
    """Range breakout. The range is the high and low of the RANGE_BARS bars
    before a candle, accepted only if its width is at most RANGE_MAX_ATR_MULT
    x ATR. A breakout is a close beyond a range edge by BREAK_BUFFER_ATR_MULT
    x ATR, within the last BREAKOUT_LOOKBACK bars. Confirmed means the
    breakout candle has displacement AND price retested the broken edge within
    RETEST_WINDOW bars and is holding above (or below) it.

    Returns None if there is no breakout. If the breakout has failed (price
    closed back inside the range), returns a dict with "failed": True so the
    guidance can exit trades in that direction."""
    if not atr_val or len(df) < RANGE_BARS + 2:
        return None
    highs, lows = df["High"].values, df["Low"].values
    closes = df["Close"].values
    n = len(df)
    close = float(closes[-1])
    buf = BREAK_BUFFER_ATR_MULT * atr_val
    tol = RETEST_TOL_ATR_MULT * atr_val
    first = max(RANGE_BARS, n - 1 - BREAKOUT_LOOKBACK)

    # newest candidate first, so the most recent breakout wins
    for i in range(n - 1, first - 1, -1):
        rng_hi = float(highs[i - RANGE_BARS:i].max())
        rng_lo = float(lows[i - RANGE_BARS:i].min())
        if rng_hi - rng_lo > RANGE_MAX_ATR_MULT * atr_val:
            continue  # not a consolidation

        if closes[i] > rng_hi + buf:
            direction, level = "up", rng_hi
        elif closes[i] < rng_lo - buf:
            direction, level = "down", rng_lo
        else:
            continue

        # failed breakout: price has closed back inside the range
        if (direction == "up" and close < level) or (direction == "down" and close > level):
            return {"failed": True, "dir": direction, "level": level,
                    "range_hi": rng_hi, "range_lo": rng_lo, "pos": i,
                    "confirmed": False, "retest": False, "displacement": False,
                    "status": "Breakout failed — closed back inside the range"}

        # retest of the broken edge, after the breakout candle, excluding the latest bar
        start = max(i + 1, n - 1 - RETEST_WINDOW)
        retest = False
        if start < n - 1:
            if direction == "up":
                touched = (lows[start:n - 1] <= level + tol) & (closes[start:n - 1] >= level)
                retest = bool(touched.any()) and close > level
            else:
                touched = (highs[start:n - 1] >= level - tol) & (closes[start:n - 1] <= level)
                retest = bool(touched.any()) and close < level

        disp = bool((highs[i] - lows[i]) >= DISPLACEMENT_ATR_MULT * atr_val)

        if disp and retest:
            status = "Confirmed breakout — displacement + retest held"
        elif retest:
            status = "Breakout retest held, but no displacement on the break"
        elif disp:
            status = "Breakout with displacement — awaiting retest"
        else:
            status = "Breakout — no displacement, awaiting retest"

        return {
            "dir": direction,
            "level": level,
            "range_hi": rng_hi,
            "range_lo": rng_lo,
            "pos": i,
            "displacement": disp,
            "retest": retest,
            "confirmed": disp and retest,
            "status": status,
        }
    return None


# ================== HIGHER TIMEFRAME ==================
def get_htf_context():
    """Higher-timeframe bias. Returns None if the HTF fetch fails (the
    post still goes out, just without the HTF line)."""
    try:
        htf_df = fetch_candles(PAIR, HTF_INTERVAL, HTF_OUTPUTSIZE)
        res = analyze_structure_deep(htf_df, use_events=False)
        return {"bias": res[1], "interval": HTF_INTERVAL.upper(), "notes": res[0]}
    except Exception as e:
        print("HTF fetch failed (continuing without it):", scrub(e))
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


# ================== STRUCTURE EVENTS / IMPULSE LEGS (chart) ==================
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
        if abs(p1 - p0) >= IMPULSE_ATR_MULT * atr_val and (i1 - i0) <= IMPULSE_MAX_BARS:
            legs.append({"x0": i0, "x1": i1, "lo": min(p0, p1), "hi": max(p0, p1),
                         "dir": 1 if t0 == "L" else -1})
    return legs[-max_legs:]


def impulse_leg(swings_high, swings_low, bias):
    """The most recent impulse in the trend direction: the swing that
    started it to the swing that ended it. Returns (start, end) or None."""
    if not len(swings_high) or not len(swings_low):
        return None
    if bias == "Bullish":
        end_idx = swings_high.index[-1]
        prior = swings_low[swings_low.index < end_idx]
        if not len(prior):
            return None
        return float(prior["Swing_Low"].iloc[-1]), float(swings_high["Swing_High"].iloc[-1])
    if bias == "Bearish":
        end_idx = swings_low.index[-1]
        prior = swings_high[swings_high.index < end_idx]
        if not len(prior):
            return None
        return float(prior["Swing_High"].iloc[-1]), float(swings_low["Swing_Low"].iloc[-1])
    return None


def volume_profile(plot_df, bins=PROFILE_BINS):
    """Horizontal profile of where price spent its time (weighted by volume
    when the feed has it). Returns (edges, up_weights, down_weights)."""
    lo = float(plot_df["Low"].min())
    hi = float(plot_df["High"].max())
    if hi <= lo:
        return np.array([lo, lo + 1]), np.zeros(1), np.zeros(1)
    edges = np.linspace(lo, hi, bins + 1)
    up = np.zeros(bins)
    dn = np.zeros(bins)
    has_vol = "Volume" in plot_df.columns and plot_df["Volume"].sum() > 0
    for o, h, l, c, v in zip(plot_df["Open"].values, plot_df["High"].values, plot_df["Low"].values,
                             plot_df["Close"].values,
                             plot_df["Volume"].values if has_vol else np.ones(len(plot_df))):
        i0 = int(np.clip(np.searchsorted(edges, l, side="right") - 1, 0, bins - 1))
        i1 = int(np.clip(np.searchsorted(edges, h, side="right") - 1, 0, bins - 1))
        share = float(v) / (i1 - i0 + 1)
        if c >= o:
            up[i0:i1 + 1] += share
        else:
            dn[i0:i1 + 1] += share
    return edges, up, dn


# ================== PHASE CLASSIFICATION ==================
def classify_phase(df, bias, break_kind, break_level, swings_high, swings_low,
                   displacement_hit, atr_val, levels, break_pos=None):
    """Returns phase, confirmed flag, confirmation text, retracement depth,
    sweep info. Invalidation Risk has an early-warning state (past 61.8% of
    the last leg, not yet through its start) and a confirmed state (closed
    through the start of the leg)."""
    close = df["Close"].iloc[-1]
    a = atr_val or 0.0
    tol = a * RETEST_TOL_ATR_MULT

    sweep = detect_liquidity_sweep(df, levels)

    retest_held = False
    if break_kind and break_level is not None and break_pos is not None:
        recent = df.iloc[max(break_pos + 1, len(df) - 1 - RETEST_WINDOW):-1]
        if len(recent):
            if bias == "Bullish":
                touched = (recent["Low"] <= break_level + tol) & (recent["Close"] >= break_level)
                retest_held = bool(touched.any() and close > break_level)
            elif bias == "Bearish":
                touched = (recent["High"] >= break_level - tol) & (recent["Close"] <= break_level)
                retest_held = bool(touched.any() and close < break_level)

    # Depth is measured against the most recent impulse (start -> end of the
    # trend move), not against the latest high/low.
    depth = None
    depth_label = None
    pull_dist = 0.0
    leg_pts = impulse_leg(swings_high, swings_low, bias)
    leg_start = leg_pts[0] if leg_pts else None
    if leg_pts:
        start, end = leg_pts
        leg = abs(end - start)
        pull_dist = (end - close) if bias == "Bullish" else (close - end)
        if leg > 0:
            depth = pull_dist / leg
            if depth < 0.382:
                depth_label = "shallow"
            elif depth <= 0.618:
                depth_label = "healthy"
            else:
                depth_label = "deep"

    # Swing structure agreement, used to confirm a Trend Leg
    trend_clean = False
    if len(swings_high) >= 2 and len(swings_low) >= 2:
        hh = swings_high["Swing_High"].iloc[-1] > swings_high["Swing_High"].iloc[-2]
        hl = swings_low["Swing_Low"].iloc[-1] > swings_low["Swing_Low"].iloc[-2]
        lh = swings_high["Swing_High"].iloc[-1] < swings_high["Swing_High"].iloc[-2]
        ll = swings_low["Swing_Low"].iloc[-1] < swings_low["Swing_Low"].iloc[-2]
        trend_clean = (bias == "Bullish" and hh and hl) or (bias == "Bearish" and lh and ll)

    phase = "Range"
    confirmed = False
    confirmation = "No confirmed direction"

    if break_kind in ("CHoCH", "BOS"):
        phase = "Reversal" if break_kind == "CHoCH" else "Continuation"
        confirmed = bool(displacement_hit and retest_held)
        if displacement_hit and retest_held:
            confirmation = "Confirmed — displacement + retest held"
        elif displacement_hit:
            confirmation = "Displacement confirmed — awaiting retest"
        elif retest_held:
            confirmation = "Retest held, but no displacement on the break"
        else:
            confirmation = "Unconfirmed — no displacement, no retest yet"

    elif bias in ("Bullish", "Bearish"):
        past_healthy = depth is not None and depth > 0.618
        if past_healthy and leg_start is not None:
            phase = "Invalidation Risk"
            broke_leg = (bias == "Bullish" and close < leg_start) or \
                        (bias == "Bearish" and close > leg_start)
            confirmed = bool(broke_leg)
            if broke_leg:
                confirmation = "Confirmed — price closed through the start of the last leg"
            else:
                confirmation = (f"Early warning — pullback past 61.8% of last leg, "
                                f"not yet through {leg_start:,.2f}")
        elif depth is not None and a and pull_dist >= PULLBACK_ATR_MULT * a:
            phase = "Pullback"
            confirmed = depth_label == "healthy"
            if confirmed:
                confirmation = f"Confirmed — healthy pullback ({depth * 100:.0f}% of last leg)"
            else:
                confirmation = f"Pullback in progress ({depth_label}, {depth * 100:.0f}% of last leg) — wait for it to hold"
        else:
            phase = "Trend Leg"
            confirmed = trend_clean
            confirmation = ("Confirmed — swing structure agrees with the trend"
                            if trend_clean else
                            "Trend intact — waiting for the next structure break")

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
        "confirmed": confirmed,
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
    """Watch level only, no order is placed. Level = nearest aligned order
    block / FVG on the pullback side of price; stop = beyond the invalidation
    level plus an ATR buffer; targets at 1R / 2R / 3R. Conditional unless the
    read is confirmed. A Trend Leg is always conditional, because a trend leg
    with no pullback is not a setup. Returns None if there's no sensible
    setup. Range breakouts are handled by build_breakout_trade()."""
    phase, bias = a["phase"], a["bias"]
    atr_val, inv = a["atr"], a["invalidation"]
    if phase in ("Range", "Invalidation Risk", "Breakout", "Confirmed Breakout") or not atr_val or not inv:
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

    conditional = (not a["confirmed"]) or phase == "Trend Leg"
    return {
        "direction": "LONG" if direction == 1 else "SHORT",
        "zone_name": zone_name,
        "zone": zone,
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "conditional": conditional,
    }


def build_breakout_trade(a, price):
    """Range breakout watch level. Zone = the broken edge +/- the retest
    tolerance. Level: current price if it is inside the zone, otherwise the
    broken edge (where a manual limit would wait for the retest). Stop: beyond
    the opposite edge of the range, or its midpoint when BREAKOUT_STOP_MODE is
    'mid', plus an ATR buffer. Targets at 1R / 2R / 3R. Conditional until the
    breakout is confirmed by displacement and a held retest."""
    rb = a["range_breakout"]
    atr_val = a["atr"]
    if not rb or not atr_val:
        return None

    d = 1 if rb["dir"] == "up" else -1
    tol = RETEST_TOL_ATR_MULT * atr_val
    level = rb["level"]
    zlo, zhi = level - tol, level + tol
    entry = price if zlo <= price <= zhi else level

    if BREAKOUT_STOP_MODE == "mid":
        ref = (rb["range_hi"] + rb["range_lo"]) / 2
    else:
        ref = rb["range_lo"] if d == 1 else rb["range_hi"]
    buffer = SL_BUFFER_ATR_MULT * atr_val
    stop = ref - buffer if d == 1 else ref + buffer

    risk = (entry - stop) * d
    if risk < MIN_RISK_ATR_MULT * atr_val or risk > MAX_RISK_ATR_MULT * atr_val:
        return None

    targets = [entry + d * risk * r for r in (1, 2, 3)]
    return {
        "direction": "LONG" if d == 1 else "SHORT",
        "zone_name": "Broken range edge",
        "zone": (zlo, zhi),
        "entry": entry,
        "stop": stop,
        "targets": targets,
        "conditional": not rb["confirmed"],
    }


def build_projection(a, price):
    """Where price may go next: the nearest liquidity pool to re-sweep, then
    two 'price range' targets beyond it (nearest levels past 1 ATR, falling
    back to ATR multiples). Only for a directional read. Returns None when
    there's no direction."""
    atr_val = a["atr"]
    if not atr_val:
        return None
    bias, phase = a["bias"], a["phase"]
    if phase == "Invalidation Risk":
        return None
    if phase == "Potential Reversal" and a["sweep"]:
        d = 1 if a["sweep"]["dir"] == "bullish" else -1
    elif bias == "Bullish":
        d = 1
    elif bias == "Bearish":
        d = -1
    else:
        return None

    pool = [(k["price"], k["kind"], k["name"]) for k in a["key_levels"]]
    for v in a["swings_high"]["Swing_High"].values:
        pool.append((float(v), "high", "Swing High"))
    for v in a["swings_low"]["Swing_Low"].values:
        pool.append((float(v), "low", "Swing Low"))
    if a["pd"]:
        pool.append((a["pd"]["eq"], "eq", "EQ"))

    if d == 1:
        # liquidity to re-sweep sits below (or right at) price
        sweeps = [p for p in pool if p[1] == "low" and p[0] <= price + 0.3 * atr_val]
        if sweeps:
            s_level, _, s_name = max(sweeps, key=lambda p: p[0])
        else:
            s_level, s_name = price - atr_val, "ATR estimate"
        targets = sorted({round(p[0], 2) for p in pool
                          if p[1] in ("high", "eq") and price + atr_val < p[0] <= price + MAX_TARGET_ATR * atr_val})
    else:
        sweeps = [p for p in pool if p[1] == "high" and p[0] >= price - 0.3 * atr_val]
        if sweeps:
            s_level, _, s_name = min(sweeps, key=lambda p: p[0])
        else:
            s_level, s_name = price + atr_val, "ATR estimate"
        targets = sorted({round(p[0], 2) for p in pool
                          if p[1] in ("low", "eq") and price - MAX_TARGET_ATR * atr_val <= p[0] < price - atr_val},
                         reverse=True)

    if targets:
        t1 = targets[0]
    else:
        t1 = price + d * PROJECTION_ATR_MULT * atr_val
    further = [t for t in targets if (t - t1) * d >= atr_val]
    t2 = further[0] if further else t1 + d * PROJECTION_ATR_MULT * atr_val

    return {"dir": d, "sweep_level": float(s_level), "sweep_name": s_name,
            "t1": float(t1), "t2": float(t2)}


def full_analysis(df, htf):
    """Runs everything once and returns one dict that both the chart and
    the message use, so they always agree."""
    (notes, bias, break_kind, break_index, break_level, swings_high,
     swings_low, order_block, displacement_hit, atr_val, break_pos) = analyze_structure_deep(df)

    levels, key_levels = build_levels(df, swings_high, swings_low, atr_val)
    ph = classify_phase(df, bias, break_kind, break_level, swings_high, swings_low,
                        displacement_hit, atr_val, levels, break_pos)

    # range breakout: only relabels a plain Range; SMC phases take priority.
    # A failed breakout is kept separately so guidance can exit trades on it.
    rb = detect_range_breakout(df, atr_val)
    range_failed = None
    if rb and rb.get("failed"):
        range_failed = rb
        rb = None
    if rb and ph["phase"] == "Range":
        ph["phase"] = "Confirmed Breakout" if rb["confirmed"] else "Breakout"
        ph["confirmation"] = rb["status"]
        ph["confirmed"] = bool(rb["confirmed"])

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
    if rb:
        extra_notes.append(
            f"Range {rb['range_lo']:,.2f} – {rb['range_hi']:,.2f} broke {rb['dir']} at {rb['level']:,.2f}"
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
        "break_pos": break_pos,
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
        "range_breakout": rb,
        "range_failed": range_failed,
        **ph,
    }
    a["projection"] = build_projection(a, price)

    open_trade, last_closed, trade_warning = load_trade_state()
    a["open_trade"] = open_trade
    a["last_closed"] = last_closed
    a["trade_warning"] = trade_warning

    # No watch level while a trade is open or the trade file can't be trusted.
    if open_trade or trade_warning:
        a["trade"] = None
    elif ph["phase"] in ("Breakout", "Confirmed Breakout"):
        a["trade"] = build_breakout_trade(a, price)
    else:
        a["trade"] = build_trade_idea(a, price)

    a["guidance"] = position_guidance(a, price, open_trade, last_closed)
    return a


# ================== CHART ==================
def _fmt_price(v, _pos=None):
    return f"{v:,.3f}"


def _fmt_countdown(df):
    """Time left in the current candle, like the badge on a trading chart."""
    start = df.index[-1].to_pydatetime()
    end = start + timedelta(minutes=interval_minutes(INTERVAL))
    secs = max(int((end - now_utc().replace(tzinfo=None)).total_seconds()), 0)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _draw_candles(ax, d, up="#26a69a", down="#ef5350"):
    x = np.arange(len(d))
    o, h, l, c = (d[k].values for k in ("Open", "High", "Low", "Close"))
    colors = np.where(c >= o, up, down)
    ax.vlines(x, l, h, colors=colors, linewidth=1.1, zorder=2)
    body = np.maximum(np.abs(c - o), (h - l) * 0.02 + 1e-6)
    ax.bar(x, body, bottom=np.minimum(o, c), width=0.62, color=colors,
           edgecolor=colors, linewidth=0.5, zorder=2.1)


def _draw_chart(df, a, filename):
    bias, phase = a["bias"], a["phase"]
    atr_val = a["atr"] or 1.0
    price = float(df["Close"].iloc[-1])
    swings_high, swings_low = a["swings_high"], a["swings_low"]
    htf = a["htf"]
    proj = a["projection"]
    rb = a["range_breakout"]

    plot_df = df.tail(CHART_BARS)
    W = len(plot_df)
    offset_start = len(df) - W
    last_x = W - 1
    xmax = last_x + 0.34 * W
    has_vol = "Volume" in plot_df.columns and plot_df["Volume"].sum() > 0

    # ---- y range (includes the projection so nothing is cut off) ----
    ys = [float(plot_df["Low"].min()), float(plot_df["High"].max())]
    if proj:
        ys += [proj["t1"], proj["t2"], proj["sweep_level"],
               proj["sweep_level"] - 2.2 * atr_val if proj["dir"] == 1 else proj["sweep_level"] + 2.2 * atr_val]
    span = max(ys) - min(ys)
    ylo = min(ys) - span * 0.12   # extra room below for the swing-low arrows
    yhi = max(ys) + span * 0.10   # extra room on top for arrows + labels

    def in_view(p):
        return ylo <= p <= yhi

    # ---- figure, gradient background ----
    fig = plt.figure(figsize=(12, 9), facecolor="#dff3f8")
    bg = fig.add_axes([0, 0, 1, 1], zorder=-5)
    grad = np.linspace(1, 0, 256).reshape(-1, 1)
    bg.imshow(grad, aspect="auto", extent=(0, 1, 0, 1),
              cmap=LinearSegmentedColormap.from_list("bg", ["#f4fbfd", "#b7e6ef"]))
    bg.axis("off")

    if has_vol:
        ax = fig.add_axes([0.025, 0.22, 0.855, 0.68])
        axv = fig.add_axes([0.025, 0.07, 0.855, 0.13], sharex=ax)
    else:
        ax = fig.add_axes([0.025, 0.08, 0.855, 0.82])
        axv = None
    for axx in [ax] + ([axv] if axv is not None else []):
        axx.patch.set_visible(False)
        for sp in axx.spines.values():
            sp.set_visible(False)

    ax.set_xlim(-1.5, xmax)
    ax.set_ylim(ylo, yhi)
    ax.yaxis.tick_right()
    ax.yaxis.set_major_formatter(FuncFormatter(_fmt_price))
    ax.tick_params(axis="y", labelsize=9, length=0, colors="#263238")
    ax.tick_params(axis="x", length=0)
    yt = ax.get_yaxis_transform()   # x in axes fraction, y in price

    # ---- time labels on the bottom axis ----
    step = max(6, int(round(W / 7)))
    ticks = list(range(0, W, step))
    labels = [plot_df.index[i].strftime("%b %d, %H:%M") for i in ticks]
    bottom_ax = axv if axv is not None else ax
    bottom_ax.set_xticks(ticks)
    bottom_ax.set_xticklabels(labels, fontsize=8, color="#546e7a")
    bottom_ax.tick_params(axis="x", length=0)
    if axv is not None:
        ax.tick_params(axis="x", labelbottom=False)
        axv.set_yticks([])

    # ---- volume profile on the right edge ----
    edges, up_w, dn_w = volume_profile(plot_df)
    tot = up_w + dn_w
    if tot.max() > 0:
        scale = 0.07 * W / tot.max()
        yc = (edges[:-1] + edges[1:]) / 2
        bh = (edges[1] - edges[0]) * 0.85
        ax.barh(yc, -dn_w * scale, left=xmax, height=bh, color="#f3b6c8", alpha=0.65, zorder=0.7)
        ax.barh(yc, -up_w * scale, left=xmax - dn_w * scale, height=bh, color="#7fcdc4", alpha=0.65, zorder=0.7)

    # ---- big structure box (earliest visible swing -> now) ----
    hi_pts, lo_pts = [], []
    for idx, v in swings_high["Swing_High"].items():
        p = df.index.get_loc(idx) - offset_start
        if p >= 0:
            hi_pts.append((p, float(v)))
    for idx, v in swings_low["Swing_Low"].items():
        p = df.index.get_loc(idx) - offset_start
        if p >= 0:
            lo_pts.append((p, float(v)))
    if hi_pts and lo_pts:
        bx0 = min(p for p, _ in hi_pts + lo_pts)
        b_hi = max(v for _, v in hi_pts)
        b_lo = min(v for _, v in lo_pts)
        ax.add_patch(plt.Rectangle((bx0 - 1, b_lo), last_x - bx0 + 1.5, b_hi - b_lo,
                                   fc="#8fd3dc", ec="none", alpha=0.22, zorder=0.4))
        if bias == "Neutral" and not rb:
            ax.text(bx0, b_hi, "Range — awaiting breakout", ha="left", va="top", fontsize=9,
                    fontweight="bold", color="#546e7a", zorder=6)

    # ---- range breakout: the consolidation box and its broken edge ----
    if rb:
        rx0 = max(rb["pos"] - RANGE_BARS - offset_start, 0)
        rx1 = max(rb["pos"] - offset_start, rx0 + 1)
        rcol = "#00838f"
        ax.add_patch(plt.Rectangle((rx0, rb["range_lo"]), last_x - rx0 + 0.5,
                                   rb["range_hi"] - rb["range_lo"],
                                   fc=rcol, ec=rcol, alpha=0.08, lw=0, zorder=0.45))
        ax.add_patch(plt.Rectangle((rx0, rb["range_lo"]), rx1 - rx0,
                                   rb["range_hi"] - rb["range_lo"],
                                   fc="none", ec=rcol, lw=1.0, ls="--", zorder=0.46))
        ax.plot([rx0, last_x], [rb["level"], rb["level"]], color=rcol, lw=1.0, ls=(0, (2, 2)), zorder=3)
        ax.text(rx0 + 0.5, rb["range_hi"], f"Range breakout {rb['dir']} ({'confirmed' if rb['confirmed'] else 'pending'})",
                fontsize=8, fontweight="bold", color=rcol, va="bottom", ha="left", zorder=6)

    # ---- impulse-leg boxes ----
    for leg in find_impulse_legs(df, a["atr"]):
        x0, x1 = leg["x0"] - offset_start, leg["x1"] - offset_start
        if x1 < 0:
            continue
        col = "#43a047" if leg["dir"] == 1 else "#e53935"
        ax.add_patch(plt.Rectangle((max(x0, 0), leg["lo"]), x1 - max(x0, 0), leg["hi"] - leg["lo"],
                                   fc=col, ec=col, alpha=0.07, lw=0, zorder=0.65))
        ax.add_patch(plt.Rectangle((max(x0, 0), leg["lo"]), x1 - max(x0, 0), leg["hi"] - leg["lo"],
                                   fc="none", ec=col, lw=1.1, zorder=0.66))

    # ---- supply / demand bands (order blocks) ----
    if bias in ("Bullish", "Bearish"):
        zc = "#a5d6a7" if bias == "Bullish" else "#ef9a9a"
        for z in a["zones"]:
            zx0 = max(z.get("pos", offset_start) - offset_start, 0)
            ax.add_patch(plt.Rectangle((zx0, z["low"]), last_x - zx0 + 0.5, z["high"] - z["low"],
                                       fc=zc, ec="none", alpha=0.55, zorder=0.6))

    # ---- fair value gaps ----
    for f in a["fvgs"]:
        fx0 = max(f["pos"] - offset_start, 0)
        col = "#1e88e5" if f["kind"] == "bullish" else "#fb8c00"
        ax.add_patch(plt.Rectangle((fx0, f["bottom"]), last_x - fx0 + 0.5, f["top"] - f["bottom"],
                                   fc=col, ec=col, alpha=0.20, lw=0, zorder=0.62))
        ax.add_patch(plt.Rectangle((fx0, f["bottom"]), last_x - fx0 + 0.5, f["top"] - f["bottom"],
                                   fc="none", ec=col, lw=0.6, ls="--", zorder=0.63))
        ax.text(fx0 + 0.5, f["top"], "FVG", fontsize=7, fontweight="bold", color=col, va="bottom", zorder=6)

    # ---- projection: re-sweep zone, price-range lines, black path ----
    if proj:
        d = proj["dir"]
        S, T1, T2 = proj["sweep_level"], proj["t1"], proj["t2"]
        good, bad = "#2e7d32", "#c62828"
        col_dir = good if d == 1 else bad

        band_lo, band_hi = ((S - 0.8 * atr_val, S - 0.05 * atr_val) if d == 1
                            else (S + 0.05 * atr_val, S + 0.8 * atr_val))
        ax.add_patch(plt.Rectangle((0, band_lo), last_x * 0.62, band_hi - band_lo,
                                   fc="#a5d6a7" if d == 1 else "#f4a6b8", ec="none", alpha=0.55, zorder=0.6))
        ax.axhline(S, color="#455a64", ls=(0, (4, 3)), lw=0.9, zorder=1.2)
        ax.text(last_x + 0.12 * W, S, "Potential Liquidity\nRe-Sweep Zone", ha="left", va="center",
                fontsize=9, color="#263238", zorder=6,
                bbox=dict(boxstyle="square,pad=0.35", fc="white", ec="#e53935", lw=1.4))
        bub_col = "#66bb6a" if d == 1 else "#e57390"
        ax.annotate("Potential Retest – Another Liquidity Sweep",
                    xy=(last_x * 0.40, S),
                    xytext=(last_x * 0.10, S - 1.5 * atr_val if d == 1 else S + 1.5 * atr_val),
                    ha="left", va="top" if d == 1 else "bottom", fontsize=10,
                    color="#2e5e32" if d == 1 else "#8e2440", zorder=6,
                    bbox=dict(boxstyle="round,pad=0.5", fc="#c8e6c9" if d == 1 else "#f8c9d4", ec=bub_col, lw=1.2),
                    arrowprops=dict(arrowstyle="-", color=bub_col, lw=1.2))

        xs0 = max(0, last_x - 0.30 * W)
        for T in (T1, T2):
            ax.plot([xs0, last_x + 0.17 * W], [T, T], color="#37474f", ls=(0, (4, 3)), lw=0.9, zorder=1.2)
            ax.text(last_x + 0.18 * W, T, "Price range", ha="left", va="center",
                    fontsize=10, color="#263238", zorder=6)

        px1, px2, px3 = last_x + 0.04 * W, last_x + 0.10 * W, last_x + 0.155 * W
        ax.plot([last_x, px1], [price, S], color="#111", lw=1.3, zorder=5)
        ax.annotate("", xy=(px2, T1), xytext=(px1, S), zorder=5,
                    arrowprops=dict(arrowstyle="-|>", color="#111", lw=1.3, mutation_scale=14))
        ax.annotate("", xy=(px3, T2), xytext=(px2, T1), zorder=5,
                    arrowprops=dict(arrowstyle="-|>", color="#111", lw=1.1, linestyle="--", mutation_scale=12))
        for lvl in (T2, S):
            ax.text(1.004, lvl, f"{lvl:,.3f}", transform=yt, ha="left", va="center", fontsize=11,
                    fontweight="bold", color="white", zorder=7,
                    bbox=dict(boxstyle="round,pad=0.35", fc=col_dir, ec="none"))

    # ---- key levels (PDH/PDL, Asia, EQH/EQL): small labels, above their line ----
    labeled = []
    for lv in a["key_levels"]:
        if not in_view(lv["price"]):
            continue
        ax.axhline(lv["price"], ls="-.", lw=0.7, color="#607d8b", alpha=0.6, zorder=1)
        near = sum(1 for p in labeled if abs(p - lv["price"]) < 0.8 * atr_val)
        labeled.append(lv["price"])
        ax.text(0.005 + 0.07 * near, lv["price"], lv["name"], transform=yt, fontsize=8, fontweight="bold",
                color="#455a64", va="bottom", ha="left", zorder=6)

    # ---- premium/discount equilibrium: label BELOW its line ----
    pdi = a["pd"]
    if pdi and in_view(pdi["eq"]):
        ax.axhline(pdi["eq"], ls=":", lw=1.0, color="#8e24aa", alpha=0.8, zorder=1)
        ax.text(0.16, pdi["eq"], "EQ 50%", transform=yt, fontsize=8, fontweight="bold",
                color="#8e24aa", va="top", ha="left", zorder=6)

    # ---- Strong High / Strong Low ----
    if bias == "Bearish" and len(swings_high):
        s_idx = swings_high["Swing_High"].idxmax()
        s_val, s_name = float(swings_high["Swing_High"].max()), "Strong High"
    elif bias == "Bullish" and len(swings_low):
        s_idx = swings_low["Swing_Low"].idxmin()
        s_val, s_name = float(swings_low["Swing_Low"].min()), "Strong Low"
    else:
        s_idx = None
    if s_idx is not None:
        sp_ = df.index.get_loc(s_idx) - offset_start
        if sp_ >= 0:
            ax.plot([sp_, last_x + 0.12 * W], [s_val, s_val], color="#111", lw=0.9, zorder=3)
            ax.text(last_x + 0.12 * W, s_val, s_name, ha="right",
                    va="bottom" if s_name == "Strong High" else "top", fontsize=8, color="#555", zorder=6)

    # ---- invalidation ----
    inv = a["invalidation"]
    if inv and in_view(inv["price"]):
        ax.axhline(inv["price"], ls=":", lw=1.3, color="#b71c1c", zorder=2)
        ax.text(0.02, inv["price"], f"Invalidation ({inv['side']}) {inv['price']:,.2f}",
                transform=yt, fontsize=8, fontweight="bold", color="#b71c1c",
                va="top" if inv["side"] == "below" else "bottom", ha="left", zorder=6)

    # ---- liquidity sweep that already happened ----
    sweep = a["sweep"]
    if sweep:
        sx = sweep["pos"] - offset_start
        if sx >= 0 and in_view(sweep["level"]):
            ax.plot([max(sx - 6, 0), last_x], [sweep["level"], sweep["level"]],
                    ls=":", lw=1.2, color="#6a1b9a", zorder=3)
            ax.text(sx, sweep["level"], f"Sweep ({sweep['name']})", fontsize=8, fontweight="bold",
                    color="#6a1b9a", ha="center", va="bottom" if sweep["side"] == "buy-side" else "top", zorder=6)

    # ---- candles ----
    _draw_candles(ax, plot_df)

    # ---- BOS / CHoCH along the visible history ----
    events = [e for e in detect_structure_events(df) if e["kind"] and e["to_pos"] >= offset_start][-6:]
    for e in events:
        xa = max(e["from_pos"] - offset_start, 0)
        xb = e["to_pos"] - offset_start
        if xb <= xa:
            continue
        ax.plot([xa, xb], [e["level"], e["level"]], color="#111", lw=0.9, ls=(0, (4, 3)), zorder=3)
        ax.text((xa + xb) / 2, e["level"], e["kind"], ha="center",
                va="bottom" if e["dir"] == "bull" else "top", fontsize=9, color="#111", zorder=6)

    # ---- swing arrows + labels (last two highs, last two lows) ----
    def swing_marker(pos, val, label, is_high):
        col = "#d32f2f" if is_high else "#2e7d32"
        ax.annotate("", xy=(pos, val), xycoords="data",
                    xytext=(0, 32 if is_high else -32), textcoords="offset points", zorder=6,
                    arrowprops=dict(arrowstyle="simple,head_width=1.2,head_length=1.0,tail_width=0.5",
                                    fc=col, ec=col, mutation_scale=16, shrinkA=0, shrinkB=3))
        # labels sit on the side with more room: left of the marker on the right half of the chart
        side = -1 if pos >= W // 2 else 1
        ax.plot([pos + side * 0.5, pos + side * 5.0], [val, val], color="#111", lw=0.8, ls=(0, (3, 3)), zorder=3)
        ax.text(pos + side * 5.4, val, label, ha="right" if side == -1 else "left", va="center",
                fontsize=11, color="#111", zorder=6)

    sh_vals = swings_high["Swing_High"]
    for i in range(max(0, len(sh_vals) - 2), len(sh_vals)):
        pos = df.index.get_loc(sh_vals.index[i]) - offset_start
        if pos < 0:
            continue
        val = float(sh_vals.iloc[i])
        label = ("Lower High" if val < sh_vals.iloc[i - 1] else "Higher High") if i >= 1 else "Swing High"
        swing_marker(pos, val, label, True)

    sl_vals = swings_low["Swing_Low"]
    for i in range(max(0, len(sl_vals) - 2), len(sl_vals)):
        pos = df.index.get_loc(sl_vals.index[i]) - offset_start
        if pos < 0:
            continue
        val = float(sl_vals.iloc[i])
        label = ("Higher Low" if val > sl_vals.iloc[i - 1] else "Lower Low") if i >= 1 else "Swing Low"
        swing_marker(pos, val, label, False)

    # ---- current price: dotted line + badges ----
    ax.axhline(price, color="#ef5350", ls=":", lw=1.0, zorder=1.5)
    ax.text(1.004, price, f"{price:,.3f}", transform=yt, ha="left", va="center", fontsize=11,
            fontweight="bold", color="white", zorder=8,
            bbox=dict(boxstyle="square,pad=0.3", fc="#111", ec="none"))
    ax.annotate(f"XAUUSD\n{_fmt_countdown(df)}", xy=(1.004, price), xycoords=yt,
                xytext=(0, -27), textcoords="offset points", ha="left", va="center",
                fontsize=9, fontweight="bold", color="white", zorder=8,
                bbox=dict(boxstyle="square,pad=0.3", fc="#ef5350", ec="none"))

    # ---- volume bars (only when the feed provides volume) ----
    if axv is not None:
        v = plot_df["Volume"].values
        cols = np.where(plot_df["Close"].values >= plot_df["Open"].values, "#26a69a", "#ef5350")
        axv.bar(np.arange(W), v, width=0.7, color=cols, alpha=0.45)
        axv.set_ylim(0, max(v.max() * 1.15, 1))
        vtxt = f"{v[-1] / 1000:.2f}K" if v[-1] >= 1000 else f"{v[-1]:.0f}"
        axv.text(1.004, v[-1], vtxt, transform=axv.get_yaxis_transform(), ha="left", va="center",
                 fontsize=9, fontweight="bold", color="white", clip_on=False,
                 bbox=dict(boxstyle="square,pad=0.3", fc="#ef5350", ec="none"))

    # ---- title + phase badge ----
    htf_txt = f" · {htf['interval']} {htf['bias']}" if htf else ""
    fig.text(0.03, 0.955, f"XAUUSD · {INTERVAL.upper()} · {bias}{htf_txt} · {a['session']}",
             fontsize=13, fontweight="bold", color="#263238", va="center")
    fig.text(0.03, 0.918, f"PHASE: {phase_label(a)}", fontsize=10, fontweight="bold", color="white",
             va="center", ha="left",
             bbox=dict(boxstyle="round,pad=0.4", fc=PHASE_COLORS.get(phase, "#616161"), ec="none"))

    fig.savefig(filename, dpi=150, facecolor=fig.get_facecolor())
    return filename


def create_chart(df, a, filename=CHART_FILE):
    """Draws the chart. Closes every open figure afterward, even when drawing
    fails, so a failed run never leaks figures into the next one."""
    try:
        return _draw_chart(df, a, filename)
    finally:
        plt.close("all")


# ================== OPEN TRADE / POSITION GUIDANCE ==================
def _valid_trade(t):
    return (isinstance(t, dict)
            and t.get("direction") in ("LONG", "SHORT")
            and isinstance(t.get("entry"), (int, float))
            and isinstance(t.get("stop"), (int, float))
            and t["entry"] != t["stop"])


def _parse_utc(s):
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_trade_state():
    """Reads open_trade.json. Format:
      {
        "updated_at":  "2026-10-20T14:30:00+00:00",
        "open":        {"direction": "LONG" or "SHORT", "entry": 4150.0,
                        "stop": 4145.0, "be_moved": false}   or null,
        "last_closed": {"direction": "LONG" or "SHORT", "entry": 4150.0,
                        "stop": 4145.0, "result": "stopped" or "closed"}   or null
      }
    Returns (open_trade, last_closed, warning).

    Safety rules:
      - A missing file means "no trade recorded", with no warning.
      - An unreadable file, or an open trade whose updated_at is missing or
        older than OPEN_TRADE_MAX_AGE_HOURS, sets a warning. While a warning
        is set, position guidance says "Check trade file" instead of giving
        an entry or hold instruction.
      - A stale file with no open trade drops last_closed, since a stale
        stop-out should not drive re-entry logic.
    Never raises."""
    empty = (None, None, None)
    try:
        with open(OPEN_TRADE_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return empty
    except Exception as e:
        return None, None, f"open_trade.json could not be read ({scrub(e)})."

    if not isinstance(data, dict):
        return None, None, "open_trade.json has the wrong format."

    op = data.get("open") if _valid_trade(data.get("open")) else None
    lc = data.get("last_closed") if _valid_trade(data.get("last_closed")) else None

    updated = _parse_utc(data.get("updated_at"))
    warning = None
    if op is not None or lc is not None:
        if updated is None:
            age_note = "has no valid updated_at field, so its age is unknown"
            stale = True
        else:
            age_h = (now_utc() - updated).total_seconds() / 3600
            stale = age_h > OPEN_TRADE_MAX_AGE_HOURS
            age_note = f"was last updated {age_h:.1f} h ago (limit {OPEN_TRADE_MAX_AGE_HOURS:g} h)"
        if stale:
            if op is not None:
                warning = (f"open_trade.json {age_note}. "
                           f"Update it to confirm the {op['direction']} is still open.")
            else:
                lc = None  # old stop-out info is not used for re-entry decisions

    return op, lc, warning


def position_guidance(a, price, open_trade, last_closed):
    """One suggested action, informational only. Orders are placed by hand.
    With a trade-file warning:  Check trade file (no entry or hold advice).
    With an open trade:         Hold / Caution / Move to breakeven / Exit / Stop reached.
    Without one:                Setup at level / Re-entry / Wait / Don't enter.
    Returns {"action", "detail", "key"}."""
    bias, phase = a["bias"], a["phase"]
    t = a["trade"]

    if a.get("trade_warning"):
        return {"action": "Check trade file",
                "detail": a["trade_warning"] + " Guidance is paused until it is updated.",
                "key": "check"}

    if open_trade:
        d = 1 if open_trade["direction"] == "LONG" else -1
        entry, stop = float(open_trade["entry"]), float(open_trade["stop"])
        be_moved = bool(open_trade.get("be_moved"))
        risk = abs(entry - stop)                        # R is always measured from the original stop
        effective_stop = entry if be_moved else stop    # after a breakeven move, the live stop is entry
        progress = (price - entry) * d / risk           # in R; negative = against us
        through_stop = (price - effective_stop) * d <= 0
        structure_against = (bias == "Bullish" and d == -1) or (bias == "Bearish" and d == 1)

        if through_stop:
            return {"action": "Stop reached",
                    "detail": f"Price is through the stop at {effective_stop:,.2f}. Record the trade as closed in {OPEN_TRADE_FILE}.",
                    "key": "stop"}

        rf = a.get("range_failed")
        if rf and ((rf["dir"] == "up" and d == 1) or (rf["dir"] == "down" and d == -1)):
            return {"action": "Exit",
                    "detail": "The range breakout in this trade's direction has failed (price closed back inside the range). Consider closing it.",
                    "key": "exit"}

        if structure_against and phase in ("Reversal", "Continuation"):
            return {"action": "Exit",
                    "detail": "A confirmed break has turned structure against the trade. Consider closing it.",
                    "key": "exit"}

        if phase == "Invalidation Risk" and not structure_against:
            if a["confirmed"]:
                return {"action": "Exit",
                        "detail": "Price has closed through the start of the last leg, so the trade's read is broken. Consider closing it.",
                        "key": "exit"}
            return {"action": "Caution",
                    "detail": a["confirmation"] + ". Consider tightening the stop or preparing to exit.",
                    "key": "caution"}

        if progress >= 1 and not be_moved:
            return {"action": "Move to breakeven",
                    "detail": f"Price is at {progress:.1f}R. Move the stop to entry {entry:,.2f}, then set be_moved to true.",
                    "key": "be"}
        if be_moved:
            detail = f"Stop is at breakeven ({entry:,.2f}) and structure still supports the trade."
        else:
            detail = f"Structure still supports the trade. Currently {progress:.1f}R, stop {stop:,.2f}."
        return {"action": "Hold", "detail": detail, "key": "hold"}

    if t is None:
        return {"action": "Don't enter",
                "detail": f"No valid setup at this price. {a['confirmation']}.",
                "key": "none"}
    if t["conditional"]:
        return {"action": "Wait",
                "detail": "A setup is forming but not confirmed yet. Wait for the structure break and retest.",
                "key": "wait"}
    if t["zone"] is not None:
        zlo, zhi = t["zone"]
        if not (zlo <= price <= zhi):
            gap = zlo - price if price < zlo else price - zhi
            return {"action": "Wait",
                    "detail": f"Setup is confirmed but price is {gap:,.2f} from the entry zone "
                              f"{zlo:,.2f} – {zhi:,.2f}. Wait for price to reach the zone.",
                    "key": "wait_zone"}
    same_as_stopped = (last_closed is not None and last_closed.get("result") == "stopped"
                       and last_closed["direction"] == t["direction"])
    if same_as_stopped:
        return {"action": "Re-entry",
                "detail": f"The last {t['direction']} was stopped out. Only consider re-entering if the retest holds, not on the first touch.",
                "key": "reentry"}
    return {"action": "Setup at level",
            "detail": (f"Confirmed {t['direction']} setup. Price is at the level {t['entry']:,.2f}, "
                       f"stop reference {t['stop']:,.2f}. Orders are placed manually. "
                       f"If you take the trade, record the fill in {OPEN_TRADE_FILE}."),
            "key": "fresh"}


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
        "Breakout": "Range edge closed through, but the retest has not confirmed it yet.",
        "Confirmed Breakout": "Range broken with displacement and a held retest. Direction now favors the breakout side.",
    }
    return lines.get(phase, "Wait for reaction at the zone.")


def phase_label(a):
    """Phase name for headers. Adds CONFIRMED only for setup phases."""
    phase = a["phase"]
    if phase.startswith("Confirmed"):
        return phase.upper()
    if phase in CONFIRMABLE_PHASES and a.get("confirmed"):
        return f"CONFIRMED {phase.upper()}"
    return phase.upper()


def create_caption(a, price):
    """Short caption for the photo (Telegram caps captions at 1024 chars;
    this stays far below that because the content is fixed-size)."""
    htf = a["htf"]
    htf_part = f" | {htf['interval']}: {htf['bias']}" if htf else ""
    return (
        f"📌 <b>XAUUSD {INTERVAL.upper()} — {phase_label(a)}</b>\n"
        f"Price <b>{price:,.2f}</b> | Bias: <b>{a['bias']}</b>{htf_part}\n"
        f"{a['confirmation']}\n"
        f"🧭 Guidance: <b>{a['guidance']['action']}</b>"
    )


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
        f"<b>Phase:</b> {phase_label(a)}\n"
        f"<b>Status:</b> {a['confirmation']}\n"
        f"<b>HTF:</b> {a['align_text']}\n"
    )
    parts.append(f"<b>Structure:</b>\n{notes_text}\n")

    # premium / discount
    pdi = a["pd"]
    if pdi:
        if pdi["label"] in ("Above range", "Below range"):
            parts.append(
                f"<b>Location:</b> {pdi['label'].lower().replace(' range', '')} the swing range "
                f"({pdi['lo']:,.2f} – {pdi['hi']:,.2f})\n"
            )
        else:
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
    rb = a["range_breakout"]
    if bias in ("Bullish", "Bearish"):
        if a["in_zone"]:
            parts.append("Price is reacting inside an order-block zone.\n")
        elif zones:
            z = min(zones, key=lambda zz: min(abs(price - zz["low"]), abs(price - zz["high"])))
            parts.append(f"Nearest order-block zone: {z['low']:,.2f} – {z['high']:,.2f}.\n")
    elif rb:
        parts.append(
            f"<b>Range:</b> {rb['range_lo']:,.2f} – {rb['range_hi']:,.2f}. "
            f"Breakout {rb['dir']} at {rb['level']:,.2f}, retest {'held' if rb['retest'] else 'not yet'}.\n"
        )
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

    # projection (matches the chart)
    proj = a["projection"]
    if proj:
        parts.append(
            f"\n<b>Liquidity re-sweep zone:</b> {proj['sweep_level']:,.2f} ({proj['sweep_name']})\n"
            f"<b>Price range targets:</b> {proj['t1']:,.2f} → {proj['t2']:,.2f}\n"
        )

    # watch level (suppressed while a trade is open or the trade file needs checking)
    t = a["trade"]
    if t:
        zone_txt = f"{t['zone_name']} {t['zone'][0]:,.2f} – {t['zone'][1]:,.2f}" if t["zone"] else t["zone_name"]
        tp = " | ".join(f"TP{i + 1} {v:,.2f}" for i, v in enumerate(t["targets"]))
        cond = "Conditional: wait for confirmation first." if t["conditional"] else "Look for a reaction at the zone."
        counter = " Counter-HTF, so size down or skip." if a["align_state"] == "counter" else ""
        parts.append(
            f"\n🎯 <b>WATCH LEVEL</b> (informational, no order placed by the bot)\n"
            f"<b>{t['direction']}</b> — {zone_txt}\n"
            f"Level ~{t['entry']:,.2f} | Stop {t['stop']:,.2f}\n"
            f"Targets if taken: {tp} (1R / 2R / 3R)\n"
            f"{cond}{counter}\n"
        )

    if a.get("trade_warning"):
        parts.append(f"\n⚠️ <b>Trade file:</b> {a['trade_warning']}\n")

    ot = a["open_trade"]
    if ot:
        risk = abs(float(ot["entry"]) - float(ot["stop"]))
        parts.append(
            f"\n<b>Open trade:</b> {ot['direction']} from {float(ot['entry']):,.2f} | "
            f"stop {float(ot['stop']):,.2f} | risk {risk:,.2f}"
            + (" | stop at breakeven" if ot.get("be_moved") else "") + "\n"
        )
    g = a["guidance"]
    parts.append(f"\n🧭 <b>POSITION GUIDANCE: {g['action'].upper()}</b>\n{g['detail']}\n")

    bias_txt = f"<b>Bias:</b> {bias} — {outlook_line(phase, bias)}"
    parts.append("\n" + bias_txt + "\n")

    return "\n".join(p.rstrip("\n") for p in parts)


def split_for_telegram(text, limit=TELEGRAM_CHUNK):
    """Split on line boundaries so no HTML tag is cut in half. Every chunk
    stays under the limit unless a single line is longer than the limit."""
    chunks, current = [], ""
    for line in text.split("\n"):
        candidate = line if not current else current + "\n" + line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


# ================== REPEAT SKIPPING ==================
def make_signature(a):
    htf_bias = a["htf"]["bias"] if a["htf"] else "NA"
    sweep_name = a["sweep"]["name"] if a["sweep"] else "none"
    rb = a["range_breakout"]
    rb_part = f"rb={rb['dir']}:{int(rb['confirmed'])}" if rb else "rb=none"
    rf = a.get("range_failed")
    rf_part = f"rf={rf['dir']}" if rf else "rf=none"
    return "|".join([
        a["phase"], a["bias"], str(a["break_kind"]), htf_bias,
        f"conf={int(a['confirmed'])}",
        f"zone={int(a['in_zone'])}", f"fvg={int(a['in_fvg'] is not None)}", f"sweep={sweep_name}",
        rb_part, rf_part, f"guide={a['guidance']['key']}",
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
        print("Could not save state:", scrub(e))


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
def _rewind(files):
    if files:
        for f in files.values():
            f.seek(0)


def _telegram_post(method, data, files=None, attempts=3):
    """Posts to Telegram with safe retries.

    Retries only when the request certainly never reached Telegram:
      - ConnectTimeout (connection never established)
      - HTTP 429 with retry_after (Telegram rejected it before processing)
    A ReadTimeout or other ambiguous failure is NOT retried, because Telegram
    may have already processed the request. Retrying those is what caused
    duplicate chart posts."""
    import requests
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    last = {"ok": False, "error": "no attempt made"}
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, data=data, files=files, timeout=(10, 60))
            last = resp.json()
            if last.get("error_code") == 429:
                wait = last.get("parameters", {}).get("retry_after", 5)
                time.sleep(wait)
                _rewind(files)
                continue
            return last
        except requests.exceptions.ConnectTimeout as e:
            last = {"ok": False, "error": scrub(e)}
        except Exception as e:
            return {"ok": False, "error": scrub(e), "ambiguous": True}
        if attempt < attempts:
            time.sleep(3 * attempt)
            _rewind(files)
    return last


def send_telegram_update(photo_path, caption, message):
    """Sends the chart with its short caption, then the full text in
    line-safe chunks. Returns (photo_ok, text_ok, detail). The post counts
    as sent once the photo is in, so a failed text message doesn't cause a
    duplicate photo."""
    with open(photo_path, "rb") as photo:
        r1 = _telegram_post("sendPhoto",
                            {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                            files={"photo": photo})
    if not r1.get("ok"):
        return False, False, r1

    for chunk in split_for_telegram(message):
        r2 = _telegram_post("sendMessage",
                            {"chat_id": CHAT_ID, "text": chunk, "parse_mode": "HTML",
                             "disable_web_page_preview": True})
        if not r2.get("ok"):
            return True, False, r2
    return True, True, {"ok": True}


# ================== MAIN ==================
def main():
    if not is_forex_market_open():
        print("Forex market is closed (weekend or skip date) — skipping this run.")
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
        photo_ok, text_ok, detail = send_telegram_update(chart_file, caption, message)

        if photo_ok:
            save_state(signature)
            text_status = "full text sent" if text_ok else f"full text FAILED: {scrub(detail)}"
            print(f"Update sent ({reason}) — phase: {analysis['phase']} / bias: {analysis['bias']} / "
                  f"guidance: {analysis['guidance']['action']} — {text_status}")
        else:
            print("Error:", scrub(detail))

    except Exception as e:
        print("Error:", scrub(e))
    finally:
        if os.path.exists(CHART_FILE):
            os.remove(CHART_FILE)


if __name__ == "__main__":
    main()
