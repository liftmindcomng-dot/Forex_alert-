"""
Multi-timeframe price-action forex signal bot.

This one script drives THREE separate strategies, selected by ENTRY_MODE
(each gets its own workflow file):

  - 4H  : trend bias, from swing-high/swing-low structure
          (higher-high + higher-low = bullish, lower-high + lower-low = bearish)
  - 15M/1H : structure break (BOS/CHoCH) in the direction of the 4H bias
  - 5M  : entry confirmation method, per ENTRY_MODE:

    STRUCTURE  (ENTRY_MODE=structure): SMC-style top-down ladder.
    1H structure break, filtered by a displacement check. A liquidity
    sweep in the 20 bars before the break is detected and scored as a
    conviction booster (see classify_conviction) rather than required.
    Candidate zones (order blocks + supply/demand) persist across runs in
    state.json with mitigation tracking. A mitigated order block is
    promoted into a "breaker block" (same range, flipped direction) and
    also kept as a "mitigation block" (same range, same direction).
    Order-block candidates require a genuine impulsive move to have
    followed them (OB_MIN_MOVE_ATR_MULT). Optionally (PD_FILTER_ENABLED /
    OTE_FILTER_ENABLED), a zone match must also fall on the discount side
    for longs / premium side for shorts, or inside the OTE Fibonacci band.
    Each break is tagged CHoCH (reverses the prior bias) or BOS (continues
    it). Entry confirms on a 5M engulfing candle, rejection wick, or fresh
    fair value gap inside any active zone, restricted to the session
    window. Each confirmed signal gets a condition label and a conviction
    score that sets its hold-duration class.

    RETEST     (ENTRY_MODE=retest): breakout + retest of the broken 15M level.

    PULLBACK   (ENTRY_MODE=pullback): 5M swing pivot, then a break back
    through it in the trend direction. SWING_ENTRY_MODE=true additionally
    requires a 50-79% retracement of the breakout leg.

Sends BUY/SELL alerts to Telegram with an entry price, a structure-based
stop loss, and TP1-TP5 (1R through 5R by default, configurable via
TP_MULTIPLES). There is no automatic time-based exit. An optional hold-time
*nudge* sends one Telegram reminder once a confirmed signal has been open
longer than its duration class's threshold.

Also sends a separate "Market Update" narrative post whenever a fresh
structure break fires on TF_STRUCTURE. It is NOT a trade signal. Toggle
with MARKET_UPDATE_ENABLED.

Optionally places a demo MT5 order via MetaApi using SL + TP1 only.

Run on a schedule (recommended: every 5 minutes) via GitHub Actions.

State (per-pair bias, active structure break/zones, persisted order
blocks + breaker blocks + mitigation blocks, pending CHoCH flag, whether
a setup has already been confirmed/alerted, hold-time nudge history, plus
cached 4H/structure results, plus a daily API usage counter) is kept in
state.json.

API USAGE: only the 5M entry candle is fetched every run. 4H is cached
for TREND_CACHE_MINUTES, structure for STRUCTURE_CACHE_MINUTES. A daily
budget guard (API_DAILY_BUDGET, default 780) stops fetching for the rest
of the UTC day before Twelve Data's free-tier 800/day cap is exceeded.
Defaults are sized for 2 pairs on a 5-minute cron. Add pairs only if you
accept that the budget guard will end the day early.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from html import escape as _esc

# ---------------- config (env vars, set as GitHub Actions secrets/variables) ----------------
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Comma-separated list, e.g. "EUR/USD,GBP/USD,USD/JPY,AUD/USD"
PAIRS = [p.strip() for p in os.environ.get(
    "FX_PAIRS", "EUR/USD,GBP/USD").split(",") if p.strip()]

TF_TREND = os.environ.get("TF_TREND", "4h")             # direction / bias
TF_STRUCTURE = os.environ.get("TF_STRUCTURE", "15min")  # structure break (BOS)
TF_ENTRY = os.environ.get("TF_ENTRY", "5min")           # pullback + confirmation

# How many bars on each side are needed to confirm a swing pivot.
SWING_LOOKBACK = int(os.environ.get("SWING_LOOKBACK", "2"))

# Small ATR-based buffer added beyond the structural SL point.
SL_BUFFER_ATR_MULT = float(os.environ.get("SL_BUFFER_ATR_MULT", "0.15"))

# TP1..TPn as R-multiples, e.g. "1,2,3,4,5" -> TP1=1R ... TP5=5R
TP_MULTIPLES = tuple(float(x) for x in os.environ.get("TP_MULTIPLES", "1,2,3,4,5").split(",") if x.strip())

# Entry confirmation method on the 5M chart:
#   "retest"    - breakout + retest.
#   "pullback"  - pure price-action pullback.
#   "structure" - SMC top-down ladder (order blocks, supply/demand,
#                 liquidity sweep, S/R confluence, displacement filter).
ENTRY_MODE = os.environ.get("ENTRY_MODE", "pullback").lower()
RETEST_TOLERANCE_ATR_MULT = float(os.environ.get("RETEST_TOLERANCE_ATR_MULT", "0.3"))

# Twelve Data free tier allows 8 req/min -> minimum ~7.5s between calls.
API_CALL_SLEEP = float(os.environ.get("API_CALL_SLEEP_SECONDS", "8"))

# Daily Twelve Data call budget for this script. The free tier caps at
# 800/day; the default leaves headroom. When reached, fetching stops for
# the rest of the UTC day and the run exits cleanly.
API_DAILY_BUDGET = int(os.environ.get("API_DAILY_BUDGET", "780"))

# Caching so 4H/structure aren't re-fetched every single cron run.
TREND_CACHE_MINUTES = int(os.environ.get("TREND_CACHE_MINUTES", "230"))  # ~4H bar (240min)
# Default depends on the structure timeframe: a 15min bar refreshes every
# 20 minutes (~72 structure calls/day/pair); a 1h bar every 50 minutes.
_DEFAULT_STRUCTURE_CACHE = 20 if "15" in TF_STRUCTURE else (50 if "1h" in TF_STRUCTURE.lower() else 30)
STRUCTURE_CACHE_MINUTES = int(os.environ.get("STRUCTURE_CACHE_MINUTES", str(_DEFAULT_STRUCTURE_CACHE)))

# How long a pending CHoCH flag (set on a 4H bias flip) waits for the
# next structure break before it expires.
CHOCH_PENDING_HOURS = float(os.environ.get("CHOCH_PENDING_HOURS", "24"))

# Send a candlestick chart image (with entry/SL/TP marked) instead of a
# plain text alert. Falls back to text if chart generation/send fails.
CHART_ENABLED = os.environ.get("CHART_ENABLED", "true").lower() == "true"
CHART_CANDLES = int(os.environ.get("CHART_CANDLES", "40"))

# For ENTRY_MODE=structure only (order block + session filter):
OB_LOOKBACK = int(os.environ.get("OB_LOOKBACK", "15"))
REJECTION_WICK_RATIO = float(os.environ.get("REJECTION_WICK_RATIO", "0.5"))
# London/NY combined session window, UTC hours (default ~07:00-21:00 UTC).
SESSION_START_UTC = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.environ.get("SESSION_END_UTC", "21"))

# Second, independently-toggleable session window (Sydney+Tokyo/Asian).
SESSION2_ENABLED = os.environ.get("SESSION2_ENABLED", "false").lower() == "true"
SESSION2_START_UTC = int(os.environ.get("SESSION2_START_UTC", "21"))
SESSION2_END_UTC = int(os.environ.get("SESSION2_END_UTC", "9"))

# For ENTRY_MODE=structure only — max unmitigated zones per direction.
OB_MAX_ZONES = int(os.environ.get("OB_MAX_ZONES", "3"))

# For ENTRY_MODE=structure only — order-block quality filter.
OB_MIN_MOVE_ATR_MULT = float(os.environ.get("OB_MIN_MOVE_ATR_MULT", "1.0"))

# For ENTRY_MODE=structure only — Premium/Discount + OTE filter.
PD_FILTER_ENABLED = os.environ.get("PD_FILTER_ENABLED", "false").lower() == "true"
OTE_FILTER_ENABLED = os.environ.get("OTE_FILTER_ENABLED", "false").lower() == "true"
OTE_RETRACE_MIN = float(os.environ.get("OTE_RETRACE_MIN", "0.62"))
OTE_RETRACE_MAX = float(os.environ.get("OTE_RETRACE_MAX", "0.79"))

# For ENTRY_MODE=structure only — liquidity pool / sweep detection.
LIQUIDITY_LOOKBACK = int(os.environ.get("LIQUIDITY_LOOKBACK", "20"))

# For ENTRY_MODE=structure only — displacement filter on the break candle.
DISPLACEMENT_ATR_MULT = float(os.environ.get("DISPLACEMENT_ATR_MULT", "1.0"))

# For ENTRY_MODE=structure only — S/R confluence.
SR_MIN_TOUCHES = int(os.environ.get("SR_MIN_TOUCHES", "0"))
SR_TOUCH_TOLERANCE_ATR_MULT = float(os.environ.get("SR_TOUCH_TOLERANCE_ATR_MULT", "0.25"))

# For ENTRY_MODE=structure only — supply/demand zone detection.
SD_CONSOLIDATION_BARS = int(os.environ.get("SD_CONSOLIDATION_BARS", "3"))
SD_MOVE_ATR_MULT = float(os.environ.get("SD_MOVE_ATR_MULT", "1.5"))

# For ENTRY_MODE=pullback only.
SWING_ENTRY_MODE = os.environ.get("SWING_ENTRY_MODE", "false").lower() == "true"
SWING_RETRACE_MIN = float(os.environ.get("SWING_RETRACE_MIN", "0.5"))
SWING_RETRACE_MAX = float(os.environ.get("SWING_RETRACE_MAX", "0.79"))

STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "")
STATE_FILENAME = os.environ.get("STATE_FILENAME", "state.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), STATE_FILENAME)

# ---- Market Update posts ----
MARKET_UPDATE_ENABLED = os.environ.get("MARKET_UPDATE_ENABLED", "true").lower() == "true"
MARKET_UPDATE_LABEL = os.environ.get("MARKET_UPDATE_LABEL", STRATEGY_LABEL or "Market")
MARKET_UPDATE_TF_LABEL = os.environ.get("MARKET_UPDATE_TF_LABEL", TF_STRUCTURE.upper())
MARKET_UPDATE_TARGET_ATR_MULTIPLES = tuple(
    float(x) for x in os.environ.get("MARKET_UPDATE_TARGET_ATR_MULTIPLES", "1.5,4.5").split(",") if x.strip()
)
MARKET_UPDATE_MIN_MOVE_ATR_MULT = float(os.environ.get("MARKET_UPDATE_MIN_MOVE_ATR_MULT", "1.0"))

# ---- hold-time nudges ----

def _parse_optional_hours(env_name):
    raw = os.environ.get(env_name, "").strip()
    return float(raw) if raw else None


DAY_MAX_HOLD_HOURS = _parse_optional_hours("DAY_MAX_HOLD_HOURS")
DAY_SWING_MAX_HOLD_HOURS = _parse_optional_hours("DAY_SWING_MAX_HOLD_HOURS")
SWING_MAX_HOLD_HOURS = _parse_optional_hours("SWING_MAX_HOLD_HOURS")
HOLD_TIME_NUDGES_ENABLED = any(
    v is not None for v in (DAY_MAX_HOLD_HOURS, DAY_SWING_MAX_HOLD_HOURS, SWING_MAX_HOLD_HOURS)
)

# ---- broker symbol mapping ----
SYMBOL_SUFFIX = os.environ.get("BROKER_SYMBOL_SUFFIX", "m")


def to_broker_symbol(pair):
    """'EUR/USD' -> 'EURUSDm', 'XAU/USD' -> 'XAUUSDm', etc."""
    return pair.replace("/", "") + SYMBOL_SUFFIX


# ---- MetaApi (MT5) demo auto-trade — OPTIONAL, off by default ----
AUTO_TRADE_ENABLED = os.environ.get("AUTO_TRADE_ENABLED", "false").lower() == "true"
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
TRADE_LOT_SIZE = float(os.environ.get("TRADE_LOT_SIZE", "0.01"))
MAX_CONCURRENT_TRADES = int(os.environ.get("MAX_CONCURRENT_TRADES", "1"))


# ---------------- API budget ----------------

class ApiBudgetExceeded(Exception):
    """Raised when today's Twelve Data call budget is spent. Not an error:
    the run stops fetching and exits cleanly until the next UTC day."""


# Per-UTC-day call counter. Persisted into state.json under "_api_usage".
_api_calls = {"date": None, "count": 0}


def _api_budget_take():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _api_calls.get("date") != today:
        _api_calls["date"] = today
        _api_calls["count"] = 0
    if _api_calls["count"] >= API_DAILY_BUDGET:
        raise ApiBudgetExceeded(
            f"Daily Twelve Data budget reached ({API_DAILY_BUDGET} calls). Skipping fetches until next UTC day.")
    _api_calls["count"] += 1


def price_decimals(pair):
    return 3 if "JPY" in pair else (2 if "XAU" in pair else 5)


# ---------------- data ----------------

def fetch_series(pair, interval, outputsize=150):
    _api_budget_take()
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": API_KEY,
        # Force UTC explicitly so the session filter's UTC-hour assumption holds.
        "timezone": "UTC",
    })
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error [{pair} {interval}]: {data.get('message', data)}")
    rows = list(reversed(data["values"]))  # oldest -> newest
    times = [r["datetime"] for r in rows]
    opens = [float(r["open"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    closes = [float(r["close"]) for r in rows]
    return times, opens, highs, lows, closes


def atr(highs, lows, closes, period=14):
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


# ---------------- swing structure ----------------

def find_swings(highs, lows, lookback=2):
    """Confirmed swing pivots only (need `lookback` bars on both sides)."""
    swings = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback:i + lookback + 1]
        window_l = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            swings.append({"i": i, "kind": "high", "price": highs[i]})
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            swings.append({"i": i, "kind": "low", "price": lows[i]})
    return swings


def last_two(swings, kind):
    matching = [s for s in swings if s["kind"] == kind]
    return matching[-2:] if len(matching) >= 2 else None


def last_swing_before(swings, kind, before_index):
    matching = [s for s in swings if s["kind"] == kind and s["i"] < before_index]
    return matching[-1] if matching else None


def get_bias(highs, lows):
    """4H trend bias from the last two confirmed swing highs/lows."""
    swings = find_swings(highs, lows, SWING_LOOKBACK)
    hh = last_two(swings, "high")
    ll = last_two(swings, "low")
    if not hh or not ll:
        return None
    if hh[-1]["price"] > hh[-2]["price"] and ll[-1]["price"] > ll[-2]["price"]:
        return "bullish"
    if hh[-1]["price"] < hh[-2]["price"] and ll[-1]["price"] < ll[-2]["price"]:
        return "bearish"
    return None


def check_structure_break(highs, lows, closes, bias, displacement_atr_mult=None, atr_val=None):
    """Structure timeframe: has price broken the most recent relevant
    swing in the direction of `bias`? Returns
    (bos_level, pullback_zone_price, bos_swing_index) or None."""
    swings = find_swings(highs, lows, SWING_LOOKBACK)
    n = len(closes)
    last_close = closes[n - 1]

    def displacement_ok():
        if not displacement_atr_mult or not atr_val:
            return True
        candle_range = highs[n - 1] - lows[n - 1]
        return candle_range >= displacement_atr_mult * atr_val

    if bias == "bullish":
        level_swing = last_swing_before(swings, "high", n - 1)
        if not level_swing:
            return None
        if last_close > level_swing["price"] and displacement_ok():
            anchor = last_swing_before(swings, "low", level_swing["i"])
            anchor_price = anchor["price"] if anchor else min(
                lows[max(0, level_swing["i"] - 10):level_swing["i"]] or [lows[0]])
            return level_swing["price"], anchor_price, level_swing["i"]
    else:
        level_swing = last_swing_before(swings, "low", n - 1)
        if not level_swing:
            return None
        if last_close < level_swing["price"] and displacement_ok():
            anchor = last_swing_before(swings, "high", level_swing["i"])
            anchor_price = anchor["price"] if anchor else max(
                highs[max(0, level_swing["i"] - 10):level_swing["i"]] or [highs[0]])
            return level_swing["price"], anchor_price, level_swing["i"]
    return None


def check_entry_confirmation(highs, lows, closes, bias, pullback_zone_price, bos_level, swing_mode):
    """5M pullback confirmation. Swing mode additionally requires the
    pullback swing to fall within the 50%-79% retracement of the leg."""
    swings = find_swings(highs, lows, SWING_LOOKBACK)
    n = len(closes)
    last_close = closes[n - 1]
    leg = bos_level - pullback_zone_price

    if bias == "bullish":
        candidates = [s for s in swings if s["kind"] == "low" and s["price"] >= pullback_zone_price]
        if swing_mode and leg > 0:
            zone_hi = bos_level - SWING_RETRACE_MIN * leg
            zone_lo = bos_level - SWING_RETRACE_MAX * leg
            candidates = [s for s in candidates if zone_lo <= s["price"] <= zone_hi]
        if not candidates:
            return None
        pivot = candidates[-1]
        window = highs[pivot["i"]:n - 1]
        pivot_high = max(window) if window else highs[pivot["i"]]
        if last_close > pivot_high:
            return {"entry": last_close, "sl_anchor": pivot["price"]}
    else:
        candidates = [s for s in swings if s["kind"] == "high" and s["price"] <= pullback_zone_price]
        if swing_mode and leg < 0:
            zone_lo = bos_level - SWING_RETRACE_MIN * leg
            zone_hi = bos_level - SWING_RETRACE_MAX * leg
            candidates = [s for s in candidates if zone_lo <= s["price"] <= zone_hi]
        if not candidates:
            return None
        pivot = candidates[-1]
        window = lows[pivot["i"]:n - 1]
        pivot_low = min(window) if window else lows[pivot["i"]]
        if last_close < pivot_low:
            return {"entry": last_close, "sl_anchor": pivot["price"]}
    return None


def check_retest_confirmation(highs, lows, closes, bias, bos_level, atr_val, lookback_bars=30):
    """5M breakout + retest of the broken 15M level."""
    n = len(closes)
    tol = RETEST_TOLERANCE_ATR_MULT * atr_val if atr_val else 0
    zone_lo, zone_hi = bos_level - tol, bos_level + tol
    start = max(0, n - lookback_bars)
    last_close = closes[n - 1]

    if bias == "bullish":
        touched = any(zone_lo <= lows[i] <= zone_hi for i in range(start, n - 1))
        if touched and last_close > bos_level:
            touch_lows = [lows[i] for i in range(start, n) if lows[i] <= zone_hi]
            sl_anchor = min(touch_lows) if touch_lows else lows[n - 2]
            return {"entry": last_close, "sl_anchor": sl_anchor}
    else:
        touched = any(zone_lo <= highs[i] <= zone_hi for i in range(start, n - 1))
        if touched and last_close < bos_level:
            touch_highs = [highs[i] for i in range(start, n) if highs[i] >= zone_lo]
            sl_anchor = max(touch_highs) if touch_highs else highs[n - 2]
            return {"entry": last_close, "sl_anchor": sl_anchor}
    return None


# ---------------- SMC concepts: zones, liquidity, S/R confluence ----------------

def find_order_blocks(opens, highs, lows, closes, bias, before_index, lookback=15, max_zones=3,
                       atr_val=None, min_move_atr_mult=None):
    """Up to `max_zones` order-block candidates before the break — the last
    opposite-colored candle before an impulsive leg in the lookback window."""
    start = max(0, before_index - lookback)
    zones = []
    for i in range(before_index - 1, start - 1, -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]
        if (bias == "bullish" and is_bearish) or (bias == "bearish" and is_bullish):
            if min_move_atr_mult and atr_val:
                following = range(i + 1, before_index)
                if bias == "bullish":
                    move = max((highs[j] for j in following), default=closes[i]) - closes[i]
                else:
                    move = closes[i] - min((lows[j] for j in following), default=closes[i])
                if move < min_move_atr_mult * atr_val:
                    continue
            zones.append({"high": highs[i], "low": lows[i], "type": "order_block"})
        if len(zones) >= max_zones:
            break
    return zones


def find_supply_demand_zone(opens, highs, lows, closes, bias, before_index, atr_val,
                             consolidation_bars, move_atr_mult, lookback=30):
    """A tight multi-bar base followed by a strong displacement move."""
    if not atr_val or consolidation_bars < 1:
        return None
    start = max(0, before_index - lookback)
    for end in range(before_index - 1, start + consolidation_bars, -1):
        base_start = end - consolidation_bars
        base_highs = highs[base_start:end]
        base_lows = lows[base_start:end]
        if not base_highs:
            continue
        base_range = max(base_highs) - min(base_lows)
        if base_range > atr_val * 0.8:
            continue
        move = closes[end] - closes[base_start]
        if bias == "bullish" and move >= move_atr_mult * atr_val:
            return {"high": max(base_highs), "low": min(base_lows), "type": "demand_zone"}
        if bias == "bearish" and move <= -move_atr_mult * atr_val:
            return {"high": max(base_highs), "low": min(base_lows), "type": "supply_zone"}
    return None


def find_liquidity_pools(swings, tolerance):
    """Cluster nearby same-kind swing points into liquidity pools."""
    pools = []
    for kind in ("high", "low"):
        pts = [s for s in swings if s["kind"] == kind]
        used = set()
        for i, s in enumerate(pts):
            if i in used:
                continue
            cluster = [s]
            for j in range(i + 1, len(pts)):
                if j in used:
                    continue
                if abs(pts[j]["price"] - s["price"]) <= tolerance:
                    cluster.append(pts[j])
                    used.add(j)
            if len(cluster) >= 2:
                pools.append({
                    "kind": kind,
                    "price": sum(c["price"] for c in cluster) / len(cluster),
                    "last_i": max(c["i"] for c in cluster),
                })
    return pools


def liquidity_swept_before_break(pools, bias, bos_index, lookback_bars):
    """True if a liquidity pool on the side opposite the breakout was taken
    shortly before the structure break."""
    opposite_kind = "low" if bias == "bullish" else "high"
    for p in pools:
        if p["kind"] != opposite_kind:
            continue
        if p["last_i"] >= bos_index:
            continue
        if bos_index - p["last_i"] > lookback_bars:
            continue
        return True
    return False


def structure_sequence_label(swings):
    """Human-readable trailing swing sequence for the market-update narrative."""
    highs = last_two(swings, "high")
    lows = last_two(swings, "low")

    high_dir = None
    low_dir = None
    if highs:
        high_dir = "lower" if highs[-1]["price"] < highs[-2]["price"] else "higher"
    if lows:
        low_dir = "lower" if lows[-1]["price"] < lows[-2]["price"] else "higher"

    if high_dir and low_dir:
        if high_dir == "lower" and low_dir == "lower":
            return "Lower Highs, Lower Lows"
        if high_dir == "higher" and low_dir == "higher":
            return "Higher Highs, Higher Lows"
        if high_dir == "lower" and low_dir == "higher":
            return "a contracting range (Lower Highs, Higher Lows)"
        return "an expanding range (Higher Highs, Lower Lows)"

    if high_dir:
        return "Lower Highs" if high_dir == "lower" else "Higher Highs"
    if low_dir:
        return "Lower Lows" if low_dir == "lower" else "Higher Lows"
    return "no clear swing sequence"


def nearest_liquidity_level(pools, swings, bias, fallback_price):
    """Liquidity zone price for the market-update narrative."""
    side = "low" if bias == "bearish" else "high"
    side_pools = [p for p in pools if p["kind"] == side]
    if side_pools:
        return max(side_pools, key=lambda p: p["last_i"])["price"]
    matching = [s for s in swings if s["kind"] == side]
    if matching:
        return matching[-1]["price"]
    return fallback_price


def market_update_targets(bias, liquidity_zone, atr_val):
    """(low, high) projected target range for the market-update narrative."""
    lo_mult = MARKET_UPDATE_TARGET_ATR_MULTIPLES[0]
    hi_mult = MARKET_UPDATE_TARGET_ATR_MULTIPLES[-1]
    atr_val = atr_val or 0
    if bias == "bearish":
        return liquidity_zone + lo_mult * atr_val, liquidity_zone + hi_mult * atr_val
    return liquidity_zone - hi_mult * atr_val, liquidity_zone - lo_mult * atr_val


def build_market_update_message(pair, bias, structure_seq, displacement_hit,
                                 liquidity_zone, atr_val, decimals, is_choch=False):
    """Narrative-style structure summary (HTML-escaped for Telegram)."""
    break_word = "CHoCH" if is_choch else "BOS"
    bos_word = f"bearish {break_word}" if bias == "bearish" else f"bullish {break_word}"
    disp_word = f"clear {bias} displacement" if displacement_hit else f"a mild {bias} push (no strong displacement)"
    rejection_word = "bullish rejection" if bias == "bearish" else "bearish rejection"
    move_word = "recover toward" if bias == "bearish" else "drop toward"

    target_lo, target_hi = market_update_targets(bias, liquidity_zone, atr_val)
    today = datetime.now(timezone.utc).strftime("%B %d").upper()
    display_pair = pair.replace("/", "")
    d = decimals

    return (
        f"📌 <b>MARKET UPDATE – {today}</b>\n\n"
        f"{_esc(MARKET_UPDATE_LABEL)}\n\n"
        f"— {_esc(display_pair)} / {_esc(MARKET_UPDATE_TF_LABEL)} —\n\n"
        f"🔥 <b>TRADING PLAN – STRUCTURE UPDATE</b>\n\n"
        f"{_esc(display_pair)} is testing the liquidity zone around <code>{liquidity_zone:.{d}f}</code> "
        f"after a sequence of {_esc(structure_seq)}, {bos_word}, and {disp_word}. "
        f"If a liquidity sweep occurs with {rejection_word}, price could {move_word} "
        f"<code>{target_lo:.{d}f}–{target_hi:.{d}f}</code>."
    )


def count_level_touches(highs, lows, level, tolerance, lookback_bars, before_index):
    start = max(0, before_index - lookback_bars)
    touches = 0
    i = start
    in_touch = False
    while i < before_index:
        touching = lows[i] - tolerance <= level <= highs[i] + tolerance
        if touching and not in_touch:
            touches += 1
        in_touch = touching
        i += 1
    return touches


def is_engulfing(opens, closes, bias, i):
    o1, c1 = opens[i - 1], closes[i - 1]
    o2, c2 = opens[i], closes[i]
    if bias == "bullish":
        return c2 > o2 and o1 > c1 and c2 >= o1 and o2 <= c1
    else:
        return c2 < o2 and o1 < c1 and c2 <= o1 and o2 >= c1


def has_rejection_wick(opens, highs, lows, closes, bias, i, zone_low, zone_high, wick_ratio=0.5):
    rng = highs[i] - lows[i]
    if rng <= 0:
        return False
    body_low, body_high = min(opens[i], closes[i]), max(opens[i], closes[i])
    if bias == "bullish":
        wick = body_low - lows[i]
        return lows[i] <= zone_high and (wick / rng) >= wick_ratio and closes[i] > body_low
    else:
        wick = highs[i] - body_high
        return highs[i] >= zone_low and (wick / rng) >= wick_ratio and closes[i] < body_high


def detect_fvg(opens, highs, lows, closes, i):
    if i < 2:
        return None
    if lows[i] > highs[i - 2] and closes[i - 1] > opens[i - 1]:
        return "bull"
    if highs[i] < lows[i - 2] and closes[i - 1] < opens[i - 1]:
        return "bear"
    return None


def compute_equilibrium(bos_level, pullback_zone):
    return (bos_level + pullback_zone) / 2


def in_discount_or_premium(price, bos_level, pullback_zone, bias):
    eq = compute_equilibrium(bos_level, pullback_zone)
    return price <= eq if bias == "bullish" else price >= eq


def in_ote_zone(price, bos_level, pullback_zone, bias, ote_min, ote_max):
    leg = abs(bos_level - pullback_zone)
    if leg <= 0:
        return False
    if bias == "bullish":
        zone_hi = bos_level - ote_min * leg
        zone_lo = bos_level - ote_max * leg
    else:
        zone_lo = bos_level + ote_min * leg
        zone_hi = bos_level + ote_max * leg
    return zone_lo <= price <= zone_hi


def in_session(iso_time, start_hour, end_hour):
    try:
        hour = int(iso_time[11:13])
    except (IndexError, ValueError):
        return True  # fail open rather than silently blocking every trade
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


def in_any_session(iso_time):
    if in_session(iso_time, SESSION_START_UTC, SESSION_END_UTC):
        return True
    if SESSION2_ENABLED and in_session(iso_time, SESSION2_START_UTC, SESSION2_END_UTC):
        return True
    return False


def check_smc_confirmation(times, opens, highs, lows, closes, bias, zones, session_start, session_end,
                            bos_level=None, pullback_zone=None):
    """5M: price trading inside a candidate zone with an engulfing candle,
    a rejection wick, or a fresh FVG in the trend direction, during session."""
    n = len(closes)
    i = n - 1
    if not in_any_session(times[i]):
        return None

    trend_word = "bull" if bias == "bullish" else "bear"
    fvg_hit = detect_fvg(opens, highs, lows, closes, i) == trend_word

    for zone in zones:
        zone_low, zone_high = zone["low"], zone["high"]
        price_in_zone = lows[i] <= zone_high and highs[i] >= zone_low
        if not price_in_zone:
            continue
        engulf = is_engulfing(opens, closes, bias, i)
        rej = has_rejection_wick(opens, highs, lows, closes, bias, i, zone_low, zone_high,
                                  wick_ratio=REJECTION_WICK_RATIO)
        if not (engulf or rej or fvg_hit):
            continue
        entry = closes[i]

        pd_hit, ote_hit = False, False
        if (PD_FILTER_ENABLED or OTE_FILTER_ENABLED) and bos_level is not None and pullback_zone is not None:
            pd_hit = in_discount_or_premium(entry, bos_level, pullback_zone, bias)
            if OTE_FILTER_ENABLED:
                ote_hit = in_ote_zone(entry, bos_level, pullback_zone, bias, OTE_RETRACE_MIN, OTE_RETRACE_MAX)
                if not ote_hit:
                    continue
            elif PD_FILTER_ENABLED and not pd_hit:
                continue

        sl_anchor = zone_low if bias == "bullish" else zone_high
        return {
            "entry": entry,
            "sl_anchor": sl_anchor,
            "zone_type": zone.get("type", "order_block"),
            "confirmations": {
                "engulfing": engulf, "rejection": rej, "fvg": fvg_hit,
                "pd": pd_hit, "ote": ote_hit,
            },
        }
    return None


# ---------------- persistent order-block / breaker-block tracking ----------------

def _zone_key(block):
    """Identity of a persisted zone: direction plus its exact price range."""
    return (block["direction"], round(block["top"], 8), round(block["bot"], 8))


def _add_unique(blocks, block):
    """Append `block` unless a zone with the same direction and range is
    already in the list (active or mitigated). Returns True if added."""
    key = _zone_key(block)
    if any(_zone_key(b) == key for b in blocks):
        return False
    blocks.append(block)
    return True


def update_order_block_mitigation(order_blocks, current_price, breaker_blocks=None, mitigation_blocks=None):
    """Deactivate any persisted order block price has fully closed through.
    The failed range is promoted into breaker_blocks (flipped direction)
    and mitigation_blocks (same direction), deduplicated by range."""
    for ob in order_blocks:
        if not ob.get("active", True):
            continue
        if ob["direction"] == "bullish" and current_price < ob["bot"]:
            ob["active"] = False
            if breaker_blocks is not None:
                _add_unique(breaker_blocks, {
                    "top": ob["top"], "bot": ob["bot"], "type": "breaker_block",
                    "direction": "bearish", "active": True, "is_choch": False,
                })
            if mitigation_blocks is not None:
                _add_unique(mitigation_blocks, {
                    "top": ob["top"], "bot": ob["bot"], "type": "mitigation_block",
                    "direction": "bullish", "active": True, "is_choch": False,
                })
        elif ob["direction"] == "bearish" and current_price > ob["top"]:
            ob["active"] = False
            if breaker_blocks is not None:
                _add_unique(breaker_blocks, {
                    "top": ob["top"], "bot": ob["bot"], "type": "breaker_block",
                    "direction": "bullish", "active": True, "is_choch": False,
                })
            if mitigation_blocks is not None:
                _add_unique(mitigation_blocks, {
                    "top": ob["top"], "bot": ob["bot"], "type": "mitigation_block",
                    "direction": "bearish", "active": True, "is_choch": False,
                })


def update_breaker_mitigation(breaker_blocks, current_price):
    """A breaker block is invalidated if price closes back fully through it."""
    for bb in breaker_blocks:
        if not bb.get("active", True):
            continue
        if bb["direction"] == "bullish" and current_price < bb["bot"]:
            bb["active"] = False
        elif bb["direction"] == "bearish" and current_price > bb["top"]:
            bb["active"] = False


def prune_order_blocks(blocks, max_zones):
    """Keep at most max_zones per direction. Inactive (mitigated) entries
    are dropped before active ones; among equals, the oldest goes first."""
    for direction in ("bullish", "bearish"):
        same = [b for b in blocks if b["direction"] == direction]
        while len(same) > max_zones:
            victim = next((b for b in same if not b.get("active", True)), same[0])
            same.remove(victim)
            blocks.remove(victim)


def sync_order_blocks(pair_state, bias, new_zones, is_choch, max_zones):
    """Persist freshly-found zones into pair_state, skipping any range that
    is already stored (so refreshes don't duplicate or resurrect zones)."""
    obs = pair_state.setdefault("order_blocks", [])
    for z in new_zones:
        _add_unique(obs, {
            "top": z["high"], "bot": z["low"], "type": z.get("type", "order_block"),
            "direction": bias, "active": True, "is_choch": is_choch,
        })
    prune_order_blocks(obs, max_zones)


def active_zones_for(pair_state, bias):
    return [
        {"high": ob["top"], "low": ob["bot"], "type": ob["type"]}
        for ob in reversed(pair_state.get("order_blocks", []))
        if ob["direction"] == bias and ob.get("active", True)
    ]


def active_breaker_zones_for(pair_state, bias):
    return [
        {"high": bb["top"], "low": bb["bot"], "type": bb["type"]}
        for bb in reversed(pair_state.get("breaker_blocks", []))
        if bb["direction"] == bias and bb.get("active", True)
    ]


def active_mitigation_zones_for(pair_state, bias):
    """Mitigation blocks only match the bias they were created under; the
    list is cleared on a bias flip."""
    return [
        {"high": mb["top"], "low": mb["bot"], "type": mb["type"]}
        for mb in reversed(pair_state.get("mitigation_blocks", []))
        if mb["direction"] == bias and mb.get("active", True)
    ]


def all_active_zones_for(pair_state, bias):
    """Entry-check order: order blocks, then breakers, then mitigation blocks."""
    return (active_zones_for(pair_state, bias)
            + active_breaker_zones_for(pair_state, bias)
            + active_mitigation_zones_for(pair_state, bias))


def has_active_zones_for_bias(pair_state, bias):
    return bool(all_active_zones_for(pair_state, bias))


def build_condition_label(is_choch, confirmations, sr_hit, sd_hit, breaker_hit=False,
                           mitigation_hit=False, pd_hit=False, ote_hit=False):
    parts = ["OB", "CHoCH" if is_choch else "BOS"]
    if confirmations.get("fvg"):
        parts.append("FVG")
    if confirmations.get("liquidity"):
        parts.append("LIQ")
    if confirmations.get("displacement"):
        parts.append("DISP")
    if confirmations.get("rejection"):
        parts.append("REJ")
    if sr_hit:
        parts.append("SR")
    if sd_hit:
        parts.append("SD")
    if breaker_hit:
        parts.append("BRK")
    if mitigation_hit:
        parts.append("MIT")
    if ote_hit:
        parts.append("OTE")
    elif pd_hit:
        parts.append("PD")
    return "+".join(parts)


def classify_conviction(is_choch, confirmations, sr_hit, sd_hit, breaker_hit=False,
                         mitigation_hit=False, ote_hit=False):
    """Conviction score -> hold-duration class ("day" / "day_swing" / "swing").
    Mitigation-block entries intentionally contribute 0."""
    score = 0
    if is_choch:
        score += 2
    if confirmations.get("displacement"):
        score += 2
    if confirmations.get("fvg"):
        score += 1
    if confirmations.get("liquidity"):
        score += 1
    if confirmations.get("rejection"):
        score += 1
    if sr_hit:
        score += 1
    if sd_hit:
        score += 1
    if breaker_hit:
        score += 1
    if ote_hit:
        score += 1

    if score >= 5:
        return "swing", "High conviction (CHoCH/displacement + multiple confluences) — manage by structure."
    elif score >= 3:
        return "day_swing", "Moderate conviction — consider partial at 1-2R, trail the rest."
    else:
        return "day", "Lower conviction, single-confirmation setup — treat as intraday."


def classify_conviction_generic(is_choch, displacement_hit, trigger, deep_retrace):
    """Lighter-weight conviction scorer for retest / pullback modes."""
    score = 0
    if is_choch:
        score += 2
    if displacement_hit:
        score += 2
    if trigger == "retest":
        score += 1
    if deep_retrace:
        score += 1

    if score >= 5:
        return "swing", "High conviction (CHoCH + displacement) — manage by structure."
    elif score >= 3:
        return "day_swing", "Moderate conviction — consider partial at 1-2R, trail the rest."
    else:
        return "day", "Lower conviction, single-confirmation setup — treat as intraday."


def build_condition_label_generic(is_choch, displacement_hit, trigger):
    parts = ["CHoCH" if is_choch else "BOS"]
    if displacement_hit:
        parts.append("DISP")
    parts.append(trigger.upper())
    return "+".join(parts)


# ---------------- state ----------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    """Atomic write: a crash mid-write leaves the previous state.json intact."""
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, STATE_FILE)


# ---------------- telegram / trading ----------------

def send_telegram(text):
    """Plain-text-safe send using Telegram's HTML parse mode. Callers must
    escape any dynamic text with _esc()."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def send_telegram_photo(image_path, caption):
    """Sends a chart image with `caption` (HTML, max 1024 chars)."""
    import requests
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
            files={"photo": f},
            timeout=30,
        )
    resp.raise_for_status()


def send_alert_with_chart(chart_path, msg):
    """Chart + full alert. If the alert exceeds the caption limit, the chart
    gets a one-line caption and the complete alert follows as a message, so
    no TP line is ever cut off."""
    if len(msg) <= 1000:
        send_telegram_photo(chart_path, msg)
    else:
        send_telegram_photo(chart_path, msg.split("\n", 1)[0])
        send_telegram(msg)


def draw_zone_rect(ax, x_start, x_end, y_low, y_high, color="#ef535030", edge_color=None):
    import matplotlib.pyplot as plt
    height = y_high - y_low
    rect = plt.Rectangle((x_start, y_low), x_end - x_start, height,
                          facecolor=color, edgecolor=edge_color or "none",
                          linewidth=1, zorder=1)
    ax.add_patch(rect)


def draw_swing_label(ax, x, y, text, above=True, color="#111"):
    offset_points = (0, 8) if above else (0, -8)
    ax.annotate(text, xy=(x, y), xycoords="data",
                xytext=offset_points, textcoords="offset points",
                fontsize=8, color=color, ha="center",
                va="bottom" if above else "top", clip_on=False)


def draw_break_marker(ax, x_break, level, x_end, label, color="#111"):
    ax.plot([x_break, x_end], [level, level], linestyle="--", linewidth=1,
            color=color, alpha=0.6, zorder=2)
    ax.annotate(label, xy=(x_break, level), xytext=(x_break, level),
                fontsize=8, color=color, ha="center", va="bottom")


def draw_target_box(ax, x, y, text, bg_color="#1b9e4b"):
    ax.annotate(
        text, xy=(x, y), fontsize=9, color="white", ha="center", va="center",
        bbox=dict(boxstyle="round,pad=0.35", fc=bg_color, ec="none"),
        zorder=5, clip_on=False, annotation_clip=False,
    )


def draw_direction_arrow(ax, x_start, y_start, x_end, y_end, color="#111"):
    ax.annotate(
        "", xy=(x_end, y_end), xytext=(x_start, y_start),
        arrowprops=dict(arrowstyle="->", color=color, linewidth=1.3, shrinkA=0, shrinkB=0),
        zorder=4, annotation_clip=False,
    )


def _break_x_in_window(times, bos_time, start):
    """Index of the break candle within the chart window (times[start:]),
    or None if the break is missing or falls before the window."""
    if not bos_time or start >= len(times):
        return None
    if bos_time < times[start]:
        return None
    for i in range(start, len(times)):
        if times[i] >= bos_time:
            return i - start
    return None


def generate_chart(pair, times, opens, highs, lows, closes, bias, signal, entry, sl, tps,
                    num_candles=40, zone=None, break_info=None):
    """Candlestick chart of the last `num_candles` entry-timeframe bars.

    break_info = {"time": <ISO time of the break candle>, "level": ..., "label": ...}
    draws the break line at the break candle's position in this window."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(closes)
    start = max(0, n - num_candles)
    count = n - start

    fig, ax = plt.subplots(figsize=(9, 5), dpi=120)
    for i in range(start, n):
        x = i - start
        up = closes[i] >= opens[i]
        color = "#26a69a" if up else "#ef5350"
        ax.plot([x, x], [lows[i], highs[i]], color=color, linewidth=1)
        body_low, body_high = min(opens[i], closes[i]), max(opens[i], closes[i])
        height = max(body_high - body_low, (highs[i] - lows[i]) * 0.02 or 0.00001)
        ax.add_patch(plt.Rectangle((x - 0.3, body_low), 0.6, height, color=color))

    if zone:
        draw_zone_rect(ax, 0, count - 1, zone["low"], zone["high"],
                        color="#ef535030" if bias == "bearish" else "#26a69a30")

    if break_info:
        x_break = _break_x_in_window(times, break_info.get("time"), start)
        if x_break is not None:
            draw_break_marker(ax, x_break, break_info["level"], count - 1, break_info["label"])

    ax.axhline(entry, color="#2962ff", linestyle="--", linewidth=1)
    ax.text(count - 1, entry, " Entry", va="center", fontsize=7, color="#2962ff")
    ax.axhline(sl, color="#d500f9", linestyle="--", linewidth=1)
    ax.text(count - 1, sl, " SL", va="center", fontsize=7, color="#d500f9")
    for idx, tp in enumerate(tps, start=1):
        ax.axhline(tp, color="#43a047", linestyle=":", linewidth=0.8)
        ax.text(count - 1, tp, f" TP{idx}", va="center", fontsize=7, color="#43a047")

    ax.set_title(f"{pair} — {signal} ({bias})", fontsize=10)
    ax.set_xlim(-1, count)
    ax.set_xticks([])
    fig.tight_layout()

    safe_pair = pair.replace("/", "")
    path = f"/tmp/chart_{safe_pair}_{int(time.time())}.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def generate_market_update_chart(pair, times, opens, highs, lows, closes, bias,
                                  swings, bos_index, bos_level, is_choch,
                                  liquidity_zone, atr_val, target_lo, target_hi,
                                  num_candles=60):
    """Annotated SMC-style chart for the Market Update post (mplfinance)."""
    import mplfinance as mpf
    import pandas as pd
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    n = len(closes)
    start = max(0, n - num_candles)
    count = n - start

    df = pd.DataFrame({
        "Open": opens[start:n],
        "High": highs[start:n],
        "Low": lows[start:n],
        "Close": closes[start:n],
    }, index=pd.to_datetime(times[start:n]))

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit", wick="inherit")
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=":",
        gridcolor="#b0bec5",
        facecolor="#eaf7fb",
        figcolor="#eaf7fb",
        y_on_right=True,
    )

    fig, axes = mpf.plot(
        df,
        type="candle",
        style=style,
        title=f"{pair} — Market Structure Update ({bias})",
        ylabel="Price",
        volume=False,
        figsize=(10, 6),
        returnfig=True,
    )
    ax = axes[0]

    trend_word = "Higher" if bias == "bullish" else "Lower"
    for s in swings:
        if s["i"] < start:
            continue
        x = s["i"] - start
        label = f"{trend_word} {'High' if s['kind'] == 'high' else 'Low'}"
        draw_swing_label(ax, x, s["price"], label, above=(s["kind"] == "high"))

    if bos_index is not None and bos_index >= start:
        label = "CHoCH" if is_choch else "BOS"
        draw_break_marker(ax, bos_index - start, bos_level, count - 1, label,
                           color="#d500f9" if is_choch else "#111")

    if liquidity_zone is not None and atr_val:
        zone_half = 0.4 * atr_val
        draw_zone_rect(ax, 0, count - 1, liquidity_zone - zone_half, liquidity_zone + zone_half,
                        color="#ef535025")
        ax.annotate("Potential Liquidity Re-Sweep Zone",
                    xy=(count * 0.35, liquidity_zone), fontsize=8, color="#c62828",
                    ha="center", va="bottom", clip_on=False)

    last_x, last_price = count - 1, closes[-1]
    mid_x = count - 1 + count * 0.15
    end_x = count - 1 + count * 0.3
    draw_direction_arrow(ax, last_x, last_price, mid_x, liquidity_zone)
    draw_direction_arrow(ax, mid_x, liquidity_zone, end_x, target_hi)
    draw_target_box(ax, end_x, target_hi, f"{target_hi:,.2f}", bg_color="#1b9e4b")
    draw_target_box(ax, mid_x, liquidity_zone, f"{liquidity_zone:,.2f}", bg_color="#1b9e4b")

    ax.set_xlim(-1, end_x + 6)

    price_lo = min(min(lows[start:n]), liquidity_zone, target_lo, target_hi)
    price_hi = max(max(highs[start:n]), liquidity_zone, target_lo, target_hi)
    pad = (price_hi - price_lo) * 0.1 or 1.0
    ax.set_ylim(price_lo - pad, price_hi + pad)

    fig.tight_layout()

    safe_pair = pair.replace("/", "")
    path = f"/tmp/marketupdate_{safe_pair}_{int(time.time())}.png"
    fig.savefig(path)
    plt.close(fig)
    return path


def place_demo_order(pair, signal, sl, tp1):
    """Market order via MetaApi using SL + TP1 only. Never raises."""
    if not (METAAPI_TOKEN and METAAPI_ACCOUNT_ID):
        return False, "MetaApi credentials not set — skipped."

    try:
        import asyncio
        from metaapi_cloud_sdk import MetaApi
    except ImportError:
        return False, "metaapi-cloud-sdk not installed."

    symbol = to_broker_symbol(pair)

    async def _place():
        api = MetaApi(METAAPI_TOKEN)
        account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
        await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()

        positions = await connection.get_positions()
        already_open = [p for p in positions if p.get("symbol") == symbol]
        if len(already_open) >= MAX_CONCURRENT_TRADES:
            return {"skipped": True, "reason": f"{len(already_open)} position(s) already open on {symbol} (limit: {MAX_CONCURRENT_TRADES})"}

        kwargs = {"stop_loss": sl, "take_profit": tp1}
        if signal == "BUY":
            result = await connection.create_market_buy_order(symbol, TRADE_LOT_SIZE, **kwargs)
        else:
            result = await connection.create_market_sell_order(symbol, TRADE_LOT_SIZE, **kwargs)
        return result

    try:
        result = asyncio.run(_place())
        if isinstance(result, dict) and result.get("skipped"):
            return False, f"Skipped — {result['reason']}."
        return True, f"Order result: {result}"
    except Exception as e:
        return False, f"MetaApi order failed: {e}"


def is_forex_market_open():
    now = datetime.now(timezone.utc)
    weekday, hour = now.weekday(), now.hour
    if weekday == 5:
        return False
    if weekday == 4 and hour >= 22:
        return False
    if weekday == 6 and hour < 22:
        return False
    return True


# ---------------- hold-time nudges ----------------

def check_hold_time_nudges(pair, setup):
    """One Telegram reminder once a confirmed signal exceeds its duration
    class's threshold. Wall-clock only; it cannot see whether the trade is
    still open."""
    if not HOLD_TIME_NUDGES_ENABLED or "confirmed_at" not in setup or setup.get("notified"):
        return
    try:
        confirmed_at = datetime.fromisoformat(setup["confirmed_at"])
    except (ValueError, TypeError):
        return
    elapsed_hours = (datetime.now(timezone.utc) - confirmed_at).total_seconds() / 3600

    limits = {
        "day": (DAY_MAX_HOLD_HOURS, "Day-trade hold window exceeded — worth reviewing this position."),
        "day_swing": (DAY_SWING_MAX_HOLD_HOURS, "Day-swing hold window exceeded — worth reviewing this position."),
        "swing": (SWING_MAX_HOLD_HOURS, "Max swing hold window exceeded — well past the usual timeframe, please review manually."),
    }
    duration_class = setup.get("duration_class", "day")
    threshold_hours, message = limits.get(duration_class, limits["day"])
    if threshold_hours is None:
        return

    if elapsed_hours >= threshold_hours:
        label = _esc(f"[{STRATEGY_LABEL}] ") if STRATEGY_LABEL else ""
        cls_label = duration_class.replace("_", "/").upper()
        send_telegram(
            f"⏰ {label}{_esc(pair)} — [{cls_label}] {_esc(message)}\n"
            f"Open for ~{elapsed_hours:.1f}h.")
        setup["notified"] = True


# ---------------- per-pair pipeline ----------------

def cache_fresh(cache, max_age_minutes):
    if not cache or "fetched_at" not in cache:
        return False
    try:
        fetched = datetime.fromisoformat(cache["fetched_at"])
    except (ValueError, TypeError):
        return False
    age_minutes = (datetime.now(timezone.utc) - fetched).total_seconds() / 60
    return age_minutes < max_age_minutes


def choch_pending(pair_state, now):
    """True if a CHoCH flag set by a recent 4H bias flip is still waiting
    for its structure break. Expires after CHOCH_PENDING_HOURS."""
    pending = pair_state.get("pending_choch")
    if not pending:
        return False
    try:
        started = datetime.fromisoformat(pending["at"])
    except (KeyError, TypeError, ValueError):
        pair_state.pop("pending_choch", None)
        return False
    if (now - started).total_seconds() / 3600 > CHOCH_PENDING_HOURS:
        pair_state.pop("pending_choch", None)
        return False
    return True


def process_pair(pair, state):
    pair_state = state.get(pair, {})
    state[pair] = pair_state  # attach up front so partial progress is saved
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    # --- 4H bias: cached, only refetched every TREND_CACHE_MINUTES ---
    trend_cache = pair_state.get("trend_cache")
    if cache_fresh(trend_cache, TREND_CACHE_MINUTES):
        bias = trend_cache["bias"]
    else:
        _, _, highs4, lows4, closes4 = fetch_series(pair, TF_TREND, outputsize=120)
        time.sleep(API_CALL_SLEEP)
        bias = get_bias(highs4, lows4)
        pair_state["trend_cache"] = {"bias": bias, "fetched_at": now_iso}

    if bias is None:
        pair_state["setup"] = None
        print(f"[{pair}] No clear 4H bias — skipping.")
        return

    prior_bias = pair_state.get("bias")
    bias_flipped = prior_bias is not None and prior_bias != bias
    if bias_flipped:
        pair_state["setup"] = None
        # Mitigation blocks never flip, so old-bias ones are cleared.
        pair_state["mitigation_blocks"] = []
        # The next structure break after a flip is a CHoCH. The flag
        # persists until that break arrives (or expires), instead of
        # existing only on the run where the flip was detected.
        pair_state["pending_choch"] = {"at": now_iso}
    pair_state["bias"] = bias

    break_is_choch = choch_pending(pair_state, now)

    # --- structure break: cached, refetched every STRUCTURE_CACHE_MINUTES
    # (and always when the 4H bias just changed) ---
    structure_cache = pair_state.get("structure_cache")
    use_cached_structure = (
        cache_fresh(structure_cache, STRUCTURE_CACHE_MINUTES)
        and structure_cache.get("bias_at_fetch") == bias
    )
    times_s = opens_s = highs_s = lows_s = closes_s = None
    swings_s = None
    bos_index = None
    if use_cached_structure:
        bos = tuple(structure_cache["bos"]) if structure_cache.get("bos") else None
        zones = structure_cache.get("zones", [])
        liquidity_ok = structure_cache.get("liquidity_ok", True)
        sr_ok = structure_cache.get("sr_ok", True)
        displacement_hit = structure_cache.get("displacement_hit", False)
        atr_s = structure_cache.get("atr")
        structure_seq = structure_cache.get("structure_seq", "no clear swing sequence")
        liquidity_zone = structure_cache.get("liquidity_zone")
        bos_time = structure_cache.get("bos_time")
    else:
        times_s, opens_s, highs_s, lows_s, closes_s = fetch_series(pair, TF_STRUCTURE, outputsize=150)
        time.sleep(API_CALL_SLEEP)
        atr_s = atr(highs_s, lows_s, closes_s, 14)
        bos = check_structure_break(
            highs_s, lows_s, closes_s, bias,
            displacement_atr_mult=(DISPLACEMENT_ATR_MULT if ENTRY_MODE == "structure" else None),
            atr_val=atr_s,
        )
        bos_time = None
        if bos:
            bos_index = bos[2]
            bos_time = times_s[bos_index]
        zones, liquidity_ok, sr_ok = [], True, True
        displacement_hit = (
            bool(atr_s) and (highs_s[-1] - lows_s[-1]) >= DISPLACEMENT_ATR_MULT * atr_s
        ) if bos else False

        swings_s = find_swings(highs_s, lows_s, SWING_LOOKBACK)
        tol_for_pools = SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
        pools_s = find_liquidity_pools(swings_s, tol_for_pools)
        structure_seq = structure_sequence_label(swings_s)
        liquidity_zone = nearest_liquidity_level(pools_s, swings_s, bias, closes_s[-1])

        if bos and ENTRY_MODE == "structure":
            bos_level, pullback_zone, bos_index = bos

            zones = find_order_blocks(opens_s, highs_s, lows_s, closes_s, bias, bos_index, OB_LOOKBACK, OB_MAX_ZONES,
                                       atr_val=atr_s, min_move_atr_mult=OB_MIN_MOVE_ATR_MULT)
            if len(zones) < OB_MAX_ZONES:
                sd_zone = find_supply_demand_zone(
                    opens_s, highs_s, lows_s, closes_s, bias, bos_index, atr_s,
                    SD_CONSOLIDATION_BARS, SD_MOVE_ATR_MULT,
                )
                if sd_zone:
                    zones.append(sd_zone)

            if LIQUIDITY_LOOKBACK > 0:
                liquidity_ok = liquidity_swept_before_break(pools_s, bias, bos_index, LIQUIDITY_LOOKBACK)

            if SR_MIN_TOUCHES > 0:
                if zones:
                    level = zones[0]["low"] if bias == "bullish" else zones[0]["high"]
                    touches = count_level_touches(highs_s, lows_s, level, tol_for_pools, len(highs_s), bos_index)
                    sr_ok = touches >= SR_MIN_TOUCHES
                else:
                    sr_ok = False

            sync_order_blocks(pair_state, bias, zones, break_is_choch, OB_MAX_ZONES)
            breakers = pair_state.setdefault("breaker_blocks", [])
            mitigations = pair_state.setdefault("mitigation_blocks", [])
            update_order_block_mitigation(pair_state.get("order_blocks", []), closes_s[-1],
                                           breaker_blocks=breakers, mitigation_blocks=mitigations)
            update_breaker_mitigation(breakers, closes_s[-1])
            prune_order_blocks(breakers, OB_MAX_ZONES)
            prune_order_blocks(mitigations, OB_MAX_ZONES)

        pair_state["structure_cache"] = {
            "bos": list(bos) if bos else None,
            "bos_time": bos_time,
            "zones": zones,
            "liquidity_ok": liquidity_ok,
            "sr_ok": sr_ok,
            "displacement_hit": displacement_hit,
            "atr": atr_s,
            "structure_seq": structure_seq,
            "liquidity_zone": liquidity_zone,
            "bias_at_fetch": bias,
            "fetched_at": now_iso,
        }

    if bos:
        bos_level, pullback_zone, bos_index_cached = bos
        if bos_index is None:
            bos_index = bos_index_cached
        current_setup = pair_state.get("setup")
        is_new_bos = not current_setup or current_setup.get("bos_level") != bos_level
        if is_new_bos:
            new_setup = {
                "bos_level": bos_level,
                "pullback_zone": pullback_zone,
                "bos_time": bos_time,
                "confirmed": False,
                "is_choch": break_is_choch,
                "displacement_hit": displacement_hit,
            }
            if ENTRY_MODE == "structure":
                new_setup["zones"] = zones
                new_setup["liquidity_ok"] = liquidity_ok
                new_setup["sr_ok"] = sr_ok
            pair_state["setup"] = new_setup
            pair_state.pop("pending_choch", None)  # consumed by this break
            print(f"[{pair}] {bias} {TF_STRUCTURE} {'CHoCH' if break_is_choch else 'BOS'} at {bos_level:.5f}. Watching {TF_ENTRY} for confirmation.")

            # Market Update post — suppressed for small same-trend moves.
            last_update = pair_state.get("last_market_update")
            should_post_update = True
            if last_update and last_update.get("bias") == bias and not break_is_choch:
                move_threshold = MARKET_UPDATE_MIN_MOVE_ATR_MULT * (atr_s or 0)
                distance = abs(bos_level - last_update.get("bos_level", bos_level))
                if move_threshold > 0 and distance < move_threshold:
                    should_post_update = False
                    print(f"[{pair}] New {bias} BOS only {distance:.5f} beyond last posted update "
                          f"(threshold {move_threshold:.5f}) — skipping Market Update, same trend continuing.")

            if MARKET_UPDATE_ENABLED and liquidity_zone is not None and should_post_update:
                decimals = price_decimals(pair)
                update_msg = build_market_update_message(
                    pair, bias, structure_seq, displacement_hit, liquidity_zone, atr_s, decimals,
                    is_choch=break_is_choch,
                )
                target_lo, target_hi = market_update_targets(bias, liquidity_zone, atr_s or 0)
                posted = False
                if not use_cached_structure and bos_index is not None:
                    try:
                        chart_path = generate_market_update_chart(
                            pair, times_s, opens_s, highs_s, lows_s, closes_s, bias,
                            swings_s, bos_index, bos_level, break_is_choch,
                            liquidity_zone, atr_s, target_lo, target_hi,
                        )
                        try:
                            send_telegram_photo(chart_path, update_msg)
                        finally:
                            os.remove(chart_path)
                        posted = True
                    except Exception as e:
                        print(f"[{pair}] Market update chart failed ({e}) — falling back to text.")
                if not posted:
                    try:
                        send_telegram(update_msg)
                        posted = True
                    except Exception as e:
                        print(f"[{pair}] Market update send failed: {e}")
                if posted:
                    pair_state["last_market_update"] = {"bias": bias, "bos_level": bos_level}
                    print(f"[{pair}] Market update posted.")

    setup = pair_state.get("setup")

    if not setup:
        print(f"[{pair}] No active setup.")
        return

    if setup.get("confirmed"):
        check_hold_time_nudges(pair, setup)
        print(f"[{pair}] Setup already confirmed — hold-time check done.")
        return

    if ENTRY_MODE == "structure":
        # Gate on zones that actually match this bias, not on leftovers
        # from the other direction (which used to let setups through to a
        # 5M fetch that could never confirm).
        if not setup.get("zones") and not has_active_zones_for_bias(pair_state, bias):
            print(f"[{pair}] No active order block / zone / breaker / mitigation for this bias — skipping.")
            return
        # Liquidity sweep is a conviction booster, not a gate.
        if not setup.get("sr_ok", True):
            print(f"[{pair}] Break level lacks S/R confluence — skipping.")
            return

    # --- 5M entry: always fetched fresh, every run ---
    times5, opens5, highs5, lows5, closes5 = fetch_series(pair, TF_ENTRY, outputsize=150)
    time.sleep(API_CALL_SLEEP)
    a5 = atr(highs5, lows5, closes5, 14) or 0

    if ENTRY_MODE == "retest":
        confirmation = check_retest_confirmation(highs5, lows5, closes5, bias, setup["bos_level"], a5)
    elif ENTRY_MODE == "structure":
        breakers = pair_state.setdefault("breaker_blocks", [])
        mitigations = pair_state.setdefault("mitigation_blocks", [])
        update_order_block_mitigation(pair_state.get("order_blocks", []), closes5[-1],
                                       breaker_blocks=breakers, mitigation_blocks=mitigations)
        update_breaker_mitigation(breakers, closes5[-1])
        active_zones = all_active_zones_for(pair_state, bias)

        if not active_zones:
            # Every persisted zone for this bias is mitigated. Re-derive
            # candidates against the current break now rather than waiting
            # for the next structure refresh.
            times_s2, opens_s2, highs_s2, lows_s2, closes_s2 = fetch_series(pair, TF_STRUCTURE, outputsize=150)
            time.sleep(API_CALL_SLEEP)
            atr_s2 = atr(highs_s2, lows_s2, closes_s2, 14)
            bos2 = check_structure_break(
                highs_s2, lows_s2, closes_s2, bias,
                displacement_atr_mult=DISPLACEMENT_ATR_MULT, atr_val=atr_s2,
            )
            if bos2:
                _, _, bos_index2 = bos2
                fresh_zones = find_order_blocks(
                    opens_s2, highs_s2, lows_s2, closes_s2, bias, bos_index2, OB_LOOKBACK, OB_MAX_ZONES,
                    atr_val=atr_s2, min_move_atr_mult=OB_MIN_MOVE_ATR_MULT)
                if len(fresh_zones) < OB_MAX_ZONES:
                    sd_zone = find_supply_demand_zone(
                        opens_s2, highs_s2, lows_s2, closes_s2, bias, bos_index2, atr_s2,
                        SD_CONSOLIDATION_BARS, SD_MOVE_ATR_MULT)
                    if sd_zone:
                        fresh_zones.append(sd_zone)
                if fresh_zones:
                    sync_order_blocks(pair_state, bias, fresh_zones, break_is_choch, OB_MAX_ZONES)
                    update_order_block_mitigation(pair_state.get("order_blocks", []), closes5[-1],
                                                   breaker_blocks=breakers, mitigation_blocks=mitigations)
                    update_breaker_mitigation(breakers, closes5[-1])
                    prune_order_blocks(breakers, OB_MAX_ZONES)
                    prune_order_blocks(mitigations, OB_MAX_ZONES)
                    active_zones = all_active_zones_for(pair_state, bias)
                    print(f"[{pair}] Re-derived {len(fresh_zones)} fresh zone(s) after mitigation.")

        if not active_zones:
            print(f"[{pair}] No active order-block/zone/breaker/mitigation remaining for this bias — skipping.")
            confirmation = None
        else:
            confirmation = check_smc_confirmation(
                times5, opens5, highs5, lows5, closes5, bias, active_zones, SESSION_START_UTC, SESSION_END_UTC,
                bos_level=setup["bos_level"], pullback_zone=setup["pullback_zone"])
    elif ENTRY_MODE == "retest_or_pullback":
        confirmation = check_retest_confirmation(highs5, lows5, closes5, bias, setup["bos_level"], a5)
        trigger = "retest"
        if not confirmation:
            confirmation = check_entry_confirmation(
                highs5, lows5, closes5, bias, setup["pullback_zone"], setup["bos_level"], SWING_ENTRY_MODE)
            trigger = "pullback"
        if confirmation:
            confirmation["trigger"] = trigger
    else:
        confirmation = check_entry_confirmation(
            highs5, lows5, closes5, bias, setup["pullback_zone"], setup["bos_level"], SWING_ENTRY_MODE)

    if not confirmation:
        print(f"[{pair}] BOS active, no {TF_ENTRY} confirmation yet.")
        return

    is_choch = setup.get("is_choch", False)
    if ENTRY_MODE == "structure":
        confs = confirmation.get("confirmations", {})
        full_confirmations = {
            "fvg": confs.get("fvg", False),
            "liquidity": setup.get("liquidity_ok", False),
            "displacement": True,  # structure break already required displacement
            "rejection": confs.get("rejection", False),
        }
        sr_hit = SR_MIN_TOUCHES > 0 and setup.get("sr_ok", False)
        sd_hit = confirmation.get("zone_type") in ("demand_zone", "supply_zone")
        breaker_hit = confirmation.get("zone_type") == "breaker_block"
        mitigation_hit = confirmation.get("zone_type") == "mitigation_block"
        pd_hit = confs.get("pd", False)
        ote_hit = confs.get("ote", False)
        confirmation["condition_label"] = build_condition_label(
            is_choch, full_confirmations, sr_hit, sd_hit, breaker_hit, mitigation_hit, pd_hit, ote_hit)
        confirmation["duration_class"], confirmation["duration_note"] = classify_conviction(
            is_choch, full_confirmations, sr_hit, sd_hit, breaker_hit, mitigation_hit, ote_hit)
    else:
        displacement_hit = setup.get("displacement_hit", False)
        trigger = confirmation.get("trigger", ENTRY_MODE)
        deep_retrace = SWING_ENTRY_MODE and trigger == "pullback"
        confirmation["condition_label"] = build_condition_label_generic(is_choch, displacement_hit, trigger)
        confirmation["duration_class"], confirmation["duration_note"] = classify_conviction_generic(
            is_choch, displacement_hit, trigger, deep_retrace)

    entry = confirmation["entry"]
    buffer = SL_BUFFER_ATR_MULT * a5

    if bias == "bullish":
        sl = confirmation["sl_anchor"] - buffer
        r = entry - sl
        tps = [entry + m * r for m in TP_MULTIPLES]
        signal = "BUY"
    else:
        sl = confirmation["sl_anchor"] + buffer
        r = sl - entry
        tps = [entry - m * r for m in TP_MULTIPLES]
        signal = "SELL"

    if r <= 0:
        print(f"[{pair}] Invalid R (SL on wrong side of entry) — skipping alert.")
        return

    decimals = price_decimals(pair)
    label_text = _esc(f"[{STRATEGY_LABEL}] ") if STRATEGY_LABEL else ""
    emoji = "🟢" if signal == "BUY" else "🔴"
    mode_desc = {
        "retest": "breakout + retest",
        "structure": f"{confirmation.get('zone_type', 'order_block').replace('_', ' ')} + engulfing/rejection/FVG (session-filtered)",
        "retest_or_pullback": f"retest+pullback mode ({confirmation.get('trigger')} fired)",
        "pullback": "deep pullback (50-79% retrace)" if SWING_ENTRY_MODE else "pullback",
    }.get(ENTRY_MODE, ENTRY_MODE)

    setup_line = ""
    if confirmation.get("duration_class"):
        cls = confirmation.get("duration_class", "day").replace("_", "/").upper()
        setup_line = (f"Setup: <code>{_esc(confirmation.get('condition_label', ''))}</code> | "
                      f"Conviction: {cls} — {_esc(confirmation.get('duration_note', ''))}\n")

    tp_lines = "\n".join(
        f"TP{idx} ({m:g}R): <code>{tp:.{decimals}f}</code>"
        for idx, (m, tp) in enumerate(zip(TP_MULTIPLES, tps), start=1)
    )
    msg = (
        f"{emoji} <b>{label_text}{signal} — {_esc(pair)}</b>\n"
        f"Bias: 4H {_esc(bias)} | Structure: {_esc(TF_STRUCTURE)} BOS <code>{setup['bos_level']:.{decimals}f}</code> | "
        f"Trigger: 5M {_esc(mode_desc)}\n"
        f"{setup_line}"
        f"Entry: <code>{entry:.{decimals}f}</code>\n"
        f"SL: <code>{sl:.{decimals}f}</code>  (R = {r:.{decimals}f})\n"
        f"{tp_lines}\n"
        f"Bar: {_esc(times5[-1])} ({_esc(TF_ENTRY)})"
    )

    # Delivery: chart first, text fallback, and the setup is marked
    # confirmed whatever happens so a failed send can't repeat next run.
    delivered = "failed"
    if CHART_ENABLED:
        try:
            chart_zone = None
            az = all_active_zones_for(pair_state, bias)
            if ENTRY_MODE == "structure" and az:
                chart_zone = az[0]
            chart_break_info = {
                "time": setup.get("bos_time"),
                "level": setup["bos_level"],
                "label": "CHoCH" if is_choch else "BOS",
            }
            chart_path = generate_chart(pair, times5, opens5, highs5, lows5, closes5, bias, signal, entry, sl, tps,
                                         CHART_CANDLES, zone=chart_zone, break_info=chart_break_info)
            try:
                send_alert_with_chart(chart_path, msg)
            finally:
                os.remove(chart_path)
            delivered = "chart"
        except Exception as e:
            print(f"[{pair}] Chart send failed ({e}) — falling back to text alert.")

    if delivered != "chart":
        try:
            send_telegram(msg)
            delivered = "text"
        except Exception as e:
            print(f"[{pair}] Text alert also failed: {e}")

    print(f"Entry alert for {pair}: delivery={delivered}.")

    setup["confirmed"] = True
    setup["delivery"] = delivered
    setup["last_entry_bar_time"] = times5[-1]
    setup["duration_class"] = confirmation.get("duration_class", "day")
    setup["condition_label"] = confirmation.get("condition_label", "")
    if HOLD_TIME_NUDGES_ENABLED:
        setup["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        setup["notified"] = False
    pair_state["setup"] = setup

    if AUTO_TRADE_ENABLED:
        filled, detail = place_demo_order(pair, signal, sl, tps[0])
        status = "✅ Demo order placed" if filled else "⚠️ Demo order NOT placed"
        try:
            send_telegram(
                f"{status} — {_esc(pair)}\n{_esc(detail)}\n"
                f"(Only TP1 is set on the order — TP2-TP{len(tps)} must be managed manually.)")
        except Exception as e:
            print(f"[{pair}] Trade status message failed: {e}")
        print(f"Demo trade [{pair}]: {status} — {detail}")


def main():
    if not is_forex_market_open():
        print("Forex market is closed (weekend) — skipping this run to avoid false signals on stale data.")
        return

    state = load_state()
    _api_calls.update({k: v for k, v in state.get("_api_usage", {}).items() if k in ("date", "count")})
    any_errors = False

    for i, pair in enumerate(PAIRS):
        if i > 0:
            time.sleep(API_CALL_SLEEP)
        try:
            process_pair(pair, state)
        except ApiBudgetExceeded as e:
            print(f"[{pair}] {e}")
            break  # no point trying further pairs; not an error
        except Exception as e:
            print(f"ERROR [{pair}]: {e}", file=sys.stderr)
            any_errors = True

    state["_api_usage"] = dict(_api_calls)
    save_state(state)

    if any_errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
