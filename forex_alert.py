"""
Multi-timeframe price-action forex signal bot.

This one script drives THREE separate strategies, selected by ENTRY_MODE
(each gets its own workflow file):

  - 4H  : trend bias, from swing-high/swing-low structure
          (higher-high + higher-low = bullish, lower-high + lower-low = bearish)
  - 15M/1H : structure break (BOS) in the direction of the 4H bias
  - 5M  : entry confirmation method, per ENTRY_MODE:

    STRUCTURE  (ENTRY_MODE=structure): SMC-style top-down ladder.
    1H structure break, filtered by a displacement check (the breaking
    candle must be an impulsive move, not a marginal poke past the
    level) and by a liquidity sweep (price must have run a cluster of
    equal highs/lows opposite the breakout direction shortly before the
    break — the "stop hunt then reversal" pattern). Candidate zones are
    then built from up to OB_MAX_ZONES order blocks (last opposite-
    colored candle before the impulse) plus one supply/demand zone (a
    tight consolidation immediately followed by a strong displacement
    move), optionally filtered further by S/R confluence (the zone edge
    must have been touched/respected SR_MIN_TOUCHES+ times historically).
    Entry confirms on a 5M engulfing candle or rejection wick inside any
    of those zones, restricted to the London/NY session window. SL
    anchors to the zone edge.

    RETEST     (ENTRY_MODE=retest): breakout + retest — price must come
    back and touch the exact broken 15M level, then close back beyond
    it in the trend direction (rejection at the level).

    PULLBACK   (ENTRY_MODE=pullback): pure price-action pullback — a 5M
    swing pivot forms, then price breaks back through it in the trend
    direction. SWING_ENTRY_MODE=true additionally requires a deep
    50-79% retracement of the breakout leg before confirming.

Sends BUY/SELL alerts to Telegram with an entry price, a structure-based
stop loss, and TP1-TP5 (1R through 5R by default, configurable via
TP_MULTIPLES). There's still no automatic time-based exit — that would
require tracking the trade's actual close, which this script doesn't do
(it only ever sends alerts / optionally opens a demo order). What IS
implemented is an optional hold-time *nudge*: once a signal has been
open longer than DAY_MAX_HOLD_HOURS / DAY_SWING_MAX_HOLD_HOURS /
SWING_MAX_HOLD_HOURS (any subset can be set — unset ones are skipped),
a one-time Telegram reminder goes out per threshold so a forgotten
trade doesn't run indefinitely unnoticed. This is a reminder based on
wall-clock time since the alert, not a real position-aware time stop.

Optionally places a demo MT5 order via MetaApi using SL + TP1 only
(MT5 orders carry a single TP field — TP2/TP3 must be managed manually,
e.g. partial closes or manual trailing).

Run on a schedule (recommended: every 5 minutes, matching the entry
timeframe) via GitHub Actions — see check-signal.yml.

State (per-pair bias, active structure break/zones, whether it's already
been confirmed/alerted, hold-time nudge history, plus cached 4H/structure
results) is kept in state.json so the same setup doesn't re-trigger a
Telegram message on every run, and so slower timeframes aren't
re-fetched every cycle.

API USAGE: with caching, only the 5M entry candle is fetched every run —
4H is cached for TREND_CACHE_MINUTES, structure for
STRUCTURE_CACHE_MINUTES. This is what makes a 5-minute cron viable on
Twelve Data's free tier (8 req/min, 800/day), but only for a small
number of pairs — 4 pairs x 2 workflows still won't fit even with
caching, since the 5M fetch alone is a hard floor. Keep FX_PAIRS short
per workflow if running on a 5-min schedule.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone

# ---------------- config (env vars, set as GitHub Actions secrets/variables) ----------------
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Comma-separated list, e.g. "EUR/USD,GBP/USD,USD/JPY,AUD/USD"
PAIRS = [p.strip() for p in os.environ.get(
    "FX_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD").split(",") if p.strip()]

TF_TREND = os.environ.get("TF_TREND", "4h")        # direction / bias
TF_STRUCTURE = os.environ.get("TF_STRUCTURE", "15min")  # structure break (BOS)
TF_ENTRY = os.environ.get("TF_ENTRY", "5min")      # pullback + confirmation

# How many bars on each side are needed to confirm a swing pivot.
SWING_LOOKBACK = int(os.environ.get("SWING_LOOKBACK", "2"))

# Small ATR-based buffer added beyond the structural SL point, so the stop
# isn't sitting exactly on the wick.
SL_BUFFER_ATR_MULT = float(os.environ.get("SL_BUFFER_ATR_MULT", "0.15"))

# TP1..TPn as R-multiples, e.g. "1,2,3,4,5" -> TP1=1R ... TP5=5R
TP_MULTIPLES = tuple(float(x) for x in os.environ.get("TP_MULTIPLES", "1,2,3,4,5").split(",") if x.strip())

# Entry confirmation method on the 5M chart, applied after a 15M/1H BOS:
#   "retest"    - breakout + retest. Used by Intraday (via retest_or_pullback).
#   "pullback"  - pure price-action pullback. Used by Swing legacy / Intraday fallback.
#   "structure" - SMC top-down ladder (order blocks, supply/demand,
#                 liquidity sweep, S/R confluence, displacement filter).
#                 Used by Swing.
ENTRY_MODE = os.environ.get("ENTRY_MODE", "pullback").lower()
RETEST_TOLERANCE_ATR_MULT = float(os.environ.get("RETEST_TOLERANCE_ATR_MULT", "0.3"))

# Twelve Data free tier allows 8 req/min -> minimum ~7.5s between calls.
# Default to 8s for margin.
API_CALL_SLEEP = float(os.environ.get("API_CALL_SLEEP_SECONDS", "8"))

# Caching so 4H/structure aren't re-fetched every single cron run — only
# the 5M entry candle is. Needed to fit a 5-min cron within the free
# tier's 800/day cap. Keep these just under the actual bar duration so a
# fresh fetch always happens at least once per bar.
TREND_CACHE_MINUTES = int(os.environ.get("TREND_CACHE_MINUTES", "230"))       # ~4H bar (240min)
STRUCTURE_CACHE_MINUTES = int(os.environ.get("STRUCTURE_CACHE_MINUTES", "50"))  # override per workflow (~12 for 15min, ~50 for 1h)

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

# For ENTRY_MODE=structure only — how many unmitigated order-block zones
# (plus one supply/demand zone, if found) to keep as live candidates.
OB_MAX_ZONES = int(os.environ.get("OB_MAX_ZONES", "3"))

# For ENTRY_MODE=structure only — liquidity pool / sweep detection.
# Two or more swing highs (or lows) within this ATR-multiple tolerance of
# each other count as one "equal highs/lows" pool. Set LIQUIDITY_LOOKBACK
# to 0 to disable the sweep requirement entirely.
LIQUIDITY_LOOKBACK = int(os.environ.get("LIQUIDITY_LOOKBACK", "20"))

# For ENTRY_MODE=structure only — the candle that breaks structure must
# have a range of at least this many ATRs, so a marginal poke past the
# level isn't treated as a real (displacement) break. Set to 0 to disable.
DISPLACEMENT_ATR_MULT = float(os.environ.get("DISPLACEMENT_ATR_MULT", "1.0"))

# For ENTRY_MODE=structure only — S/R confluence. The zone edge used for
# entry must have been touched/respected at least this many times
# historically (within SR_TOUCH_TOLERANCE_ATR_MULT). 0 disables the check.
SR_MIN_TOUCHES = int(os.environ.get("SR_MIN_TOUCHES", "0"))
SR_TOUCH_TOLERANCE_ATR_MULT = float(os.environ.get("SR_TOUCH_TOLERANCE_ATR_MULT", "0.25"))

# For ENTRY_MODE=structure only — supply/demand zone detection: a tight
# consolidation of SD_CONSOLIDATION_BARS bars followed by a move of at
# least SD_MOVE_ATR_MULT * ATR counts as an additional candidate zone,
# alongside order blocks.
SD_CONSOLIDATION_BARS = int(os.environ.get("SD_CONSOLIDATION_BARS", "3"))
SD_MOVE_ATR_MULT = float(os.environ.get("SD_MOVE_ATR_MULT", "1.5"))

# For ENTRY_MODE=pullback only:
# false = first valid pullback confirms; true = requires a deeper 50-79%
# retracement of the breakout leg before confirming (used by Swing).
SWING_ENTRY_MODE = os.environ.get("SWING_ENTRY_MODE", "false").lower() == "true"
SWING_RETRACE_MIN = float(os.environ.get("SWING_RETRACE_MIN", "0.5"))
SWING_RETRACE_MAX = float(os.environ.get("SWING_RETRACE_MAX", "0.79"))

STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "")
STATE_FILENAME = os.environ.get("STATE_FILENAME", "state.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), STATE_FILENAME)

# ---- optional hold-time nudges (any subset can be set; unset = disabled) ----

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


# ---------------- data ----------------

def fetch_series(pair, interval, outputsize=150):
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": API_KEY,
        # Force UTC explicitly — Twelve Data defaults to exchange-local
        # time when this is omitted, which would silently break the
        # session filter's UTC-hour assumption (and the weekend-close
        # check's bar-time comparisons).
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
    """Confirmed swing pivots only (need `lookback` bars on both sides,
    so still-forming bars near the end are never labeled a pivot)."""
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
    (bos_level, pullback_zone_price, bos_swing_index) or None.
    pullback_zone_price is the prior opposite swing — pullbacks should not
    trade back beyond it without invalidating the setup. bos_swing_index
    is the bar index of the broken swing, used to locate the order block.

    If `displacement_atr_mult` and `atr_val` are given, the breaking
    candle's full range must be at least `displacement_atr_mult` * ATR —
    filters out a marginal poke past the level from a genuine impulsive
    (displacement) break. Used by ENTRY_MODE=structure only; leave
    displacement_atr_mult=None to skip the check (retest/pullback modes)."""
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
    """5M: find a pullback swing, then check whether the latest close has
    broken back through it in the trend direction — that's the entry
    trigger.

    Intraday mode: any pullback swing that hasn't retraced past the prior
    opposite structure point (pullback_zone_price) qualifies.

    Swing mode: the pullback swing must additionally fall within the
    50%-79% retracement zone of the breakout leg (bos_level back toward
    pullback_zone_price) — a deeper, later pullback."""
    swings = find_swings(highs, lows, SWING_LOOKBACK)
    n = len(closes)
    last_close = closes[n - 1]
    leg = bos_level - pullback_zone_price  # positive for bullish, negative for bearish

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
    """5M: breakout + retest. After the 15M level breaks, wait for price to
    come back and actually touch that level (within a small ATR tolerance —
    the "retest"), then confirm on a close back beyond it in the trend
    direction (the level holding as new support/resistance)."""
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
# Used by ENTRY_MODE=structure. Mirrors the top-down ladder: 4H bias ->
# 1H displacement break, confirmed by a prior liquidity sweep -> order
# block / supply-demand zones, optionally filtered by S/R confluence ->
# 5M engulfing/rejection confirmation inside a zone, during London/NY
# session hours.

def find_order_blocks(opens, highs, lows, closes, bias, before_index, lookback=15, max_zones=3):
    """Up to `max_zones` order-block candidates before the break — each
    is the last opposite-colored candle before an impulsive leg within
    this lookback window (for a bullish break: the last bearish candle
    before an up-move; for bearish: the last bullish candle before a
    down-move). Ordered nearest-to-the-break first."""
    start = max(0, before_index - lookback)
    zones = []
    for i in range(before_index - 1, start - 1, -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]
        if (bias == "bullish" and is_bearish) or (bias == "bearish" and is_bullish):
            zones.append({"high": highs[i], "low": lows[i], "type": "order_block"})
        if len(zones) >= max_zones:
            break
    return zones


def find_supply_demand_zone(opens, highs, lows, closes, bias, before_index, atr_val,
                             consolidation_bars, move_atr_mult, lookback=30):
    """A supply/demand zone: a tight multi-bar consolidation (base)
    immediately followed by a strong displacement move of at least
    `move_atr_mult` * ATR in the trend direction. The zone is the price
    range of the consolidation base — price returning to it is treated
    the same way an order block is (a place the move originated from)."""
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
        if base_range > atr_val * 0.8:  # must actually be a tight base
            continue
        move = closes[end] - closes[base_start]
        if bias == "bullish" and move >= move_atr_mult * atr_val:
            return {"high": max(base_highs), "low": min(base_lows), "type": "demand_zone"}
        if bias == "bearish" and move <= -move_atr_mult * atr_val:
            return {"high": max(base_highs), "low": min(base_lows), "type": "supply_zone"}
    return None


def find_liquidity_pools(swings, tolerance):
    """Cluster nearby same-kind swing points into 'liquidity pools' —
    levels price has reacted at more than once within `tolerance`, which
    is where stop-loss/pending orders are assumed to cluster (equal
    highs / equal lows). Returns a list of {"kind","price","last_i"}."""
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
    """True if, within `lookback_bars` before the structure break, price
    took out (swept) a liquidity pool on the side opposite the breakout
    direction — e.g. for a bullish break, a cluster of equal lows got run
    first. That "stop hunt then reversal" is the confluence the SMC
    playbook wants before trusting the break."""
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


def count_level_touches(highs, lows, level, tolerance, lookback_bars, before_index):
    """How many times price has come within `tolerance` of `level` in the
    `lookback_bars` bars before `before_index` — used as S/R confluence: a
    level that's been respected multiple times is more meaningful than an
    arbitrary swing point. Consecutive touching bars only count once."""
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
    """Candle i's body engulfs candle i-1's body, in the trend direction."""
    o1, c1 = opens[i - 1], closes[i - 1]
    o2, c2 = opens[i], closes[i]
    if bias == "bullish":
        return c2 > o2 and o1 > c1 and c2 >= o1 and o2 <= c1
    else:
        return c2 < o2 and o1 < c1 and c2 <= o1 and o2 >= c1


def has_rejection_wick(opens, highs, lows, closes, bias, i, zone_low, zone_high, wick_ratio=0.5):
    """Candle i wicks into the zone but closes back out, with the wick
    making up at least `wick_ratio` of the candle's full range."""
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


def in_session(iso_time, start_hour, end_hour):
    """London/NY session filter (UTC hours). fetch_series requests
    timezone=UTC explicitly, so iso_time is guaranteed to be UTC here."""
    try:
        hour = int(iso_time[11:13])
    except (IndexError, ValueError):
        return True  # fail open rather than silently blocking every trade
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour  # wraps past midnight


def check_smc_confirmation(times, opens, highs, lows, closes, bias, zones, session_start, session_end):
    """5M: price trading inside any candidate zone (order block or
    supply/demand), with either an engulfing candle or a rejection wick
    in the trend direction, during the configured session window. Zones
    are checked nearest-to-the-break first; the first one that matches
    wins."""
    n = len(closes)
    i = n - 1
    if not in_session(times[i], session_start, session_end):
        return None

    for zone in zones:
        zone_low, zone_high = zone["low"], zone["high"]
        price_in_zone = lows[i] <= zone_high and highs[i] >= zone_low
        if not price_in_zone:
            continue
        if not (is_engulfing(opens, closes, bias, i) or
                has_rejection_wick(opens, highs, lows, closes, bias, i, zone_low, zone_high)):
            continue
        entry = closes[i]
        sl_anchor = zone_low if bias == "bullish" else zone_high
        return {"entry": entry, "sl_anchor": sl_anchor, "zone_type": zone.get("type", "order_block")}
    return None


# ---------------- state ----------------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------- telegram / trading ----------------

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
        "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def generate_chart(pair, opens, highs, lows, closes, bias, signal, entry, sl, tps, num_candles=40):
    """Candlestick chart of the last `num_candles` entry-timeframe bars,
    with entry/SL/TP levels drawn as horizontal lines. Requires
    matplotlib (imported lazily so it's only needed when charts are on)."""
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


def send_telegram_photo(image_path, caption):
    """Sends a chart image with the alert text as the caption. Requires
    `requests` (imported lazily, same reasoning as generate_chart)."""
    import requests
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(image_path, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": CHAT_ID, "caption": caption[:1024], "parse_mode": "Markdown"},
            files={"photo": f},
            timeout=30,
        )
    resp.raise_for_status()


def place_demo_order(pair, signal, sl, tp1):
    """
    Places a market order via MetaApi on whichever MT5 account is linked
    to METAAPI_ACCOUNT_ID. Uses SL + TP1 only — MT5 orders carry a single
    TP field, so TP2/TP3 must be managed manually (partial close /
    trailing). Returns (success: bool, message: str) — never raises.
    """
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
    """If a confirmed signal has been open a while, nudge the user via
    Telegram at escalating hold-duration thresholds so a forgotten trade
    doesn't run indefinitely. Only thresholds with a value set
    (DAY_MAX_HOLD_HOURS / DAY_SWING_MAX_HOLD_HOURS / SWING_MAX_HOLD_HOURS)
    are used. Each threshold notifies once (tracked in `setup`, which is
    part of state.json).

    NOTE: this only measures wall-clock time since the alert was sent —
    it has no visibility into whether the trade is actually still open
    (that lives on the broker/MT5 side, or in the user's own tracking),
    so treat it as a "go check on this" reminder, not a real exit."""
    if not HOLD_TIME_NUDGES_ENABLED or "confirmed_at" not in setup:
        return
    try:
        confirmed_at = datetime.fromisoformat(setup["confirmed_at"])
    except (ValueError, TypeError):
        return
    elapsed_hours = (datetime.now(timezone.utc) - confirmed_at).total_seconds() / 3600
    notified = set(setup.get("notified_thresholds", []))

    thresholds = [
        ("day", DAY_MAX_HOLD_HOURS, "Day-trade hold window exceeded — worth reviewing this position."),
        ("day_swing", DAY_SWING_MAX_HOLD_HOURS, "Day-swing hold window exceeded — worth reviewing this position."),
        ("swing", SWING_MAX_HOLD_HOURS, "Max swing hold window exceeded — well past the usual timeframe, please review manually."),
    ]
    label = f"[{STRATEGY_LABEL}] " if STRATEGY_LABEL else ""
    for key, threshold_hours, message in thresholds:
        if threshold_hours is None or key in notified:
            continue
        if elapsed_hours >= threshold_hours:
            send_telegram(f"⏰ {label}{pair} — {message}\nOpen for ~{elapsed_hours:.1f}h.")
            notified.add(key)
    setup["notified_thresholds"] = sorted(notified)


# ---------------- per-pair pipeline ----------------

def cache_fresh(cache, max_age_minutes):
    """True if `cache` has a fetched_at timestamp younger than max_age_minutes."""
    if not cache or "fetched_at" not in cache:
        return False
    try:
        fetched = datetime.fromisoformat(cache["fetched_at"])
    except (ValueError, TypeError):
        return False
    age_minutes = (datetime.now(timezone.utc) - fetched).total_seconds() / 60
    return age_minutes < max_age_minutes


def process_pair(pair, state):
    pair_state = state.get(pair, {})
    now_iso = datetime.now(timezone.utc).isoformat()

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
        state[pair] = pair_state
        print(f"[{pair}] No clear 4H bias — skipping.")
        return

    if pair_state.get("bias") != bias:
        pair_state["setup"] = None  # bias flipped, drop any stale setup
    pair_state["bias"] = bias

    # --- structure break: cached, only refetched every STRUCTURE_CACHE_MINUTES
    # (and always refetched if the 4H bias just changed) ---
    structure_cache = pair_state.get("structure_cache")
    use_cached_structure = (
        cache_fresh(structure_cache, STRUCTURE_CACHE_MINUTES)
        and structure_cache.get("bias_at_fetch") == bias
    )
    if use_cached_structure:
        bos = tuple(structure_cache["bos"]) if structure_cache.get("bos") else None
        zones = structure_cache.get("zones", [])
        liquidity_ok = structure_cache.get("liquidity_ok", True)
        sr_ok = structure_cache.get("sr_ok", True)
    else:
        times_s, opens_s, highs_s, lows_s, closes_s = fetch_series(pair, TF_STRUCTURE, outputsize=150)
        time.sleep(API_CALL_SLEEP)
        atr_s = atr(highs_s, lows_s, closes_s, 14)
        bos = check_structure_break(
            highs_s, lows_s, closes_s, bias,
            displacement_atr_mult=(DISPLACEMENT_ATR_MULT if ENTRY_MODE == "structure" else None),
            atr_val=atr_s,
        )
        zones, liquidity_ok, sr_ok = [], True, True
        if bos and ENTRY_MODE == "structure":
            bos_level, pullback_zone, bos_index = bos

            zones = find_order_blocks(opens_s, highs_s, lows_s, closes_s, bias, bos_index, OB_LOOKBACK, OB_MAX_ZONES)
            if len(zones) < OB_MAX_ZONES:
                sd_zone = find_supply_demand_zone(
                    opens_s, highs_s, lows_s, closes_s, bias, bos_index, atr_s,
                    SD_CONSOLIDATION_BARS, SD_MOVE_ATR_MULT,
                )
                if sd_zone:
                    zones.append(sd_zone)

            if LIQUIDITY_LOOKBACK > 0:
                tol = SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
                swings_s = find_swings(highs_s, lows_s, SWING_LOOKBACK)
                pools = find_liquidity_pools(swings_s, tol)
                liquidity_ok = liquidity_swept_before_break(pools, bias, bos_index, LIQUIDITY_LOOKBACK)

            if SR_MIN_TOUCHES > 0:
                if zones:
                    level = zones[0]["low"] if bias == "bullish" else zones[0]["high"]
                    tol = SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
                    touches = count_level_touches(highs_s, lows_s, level, tol, len(highs_s), bos_index)
                    sr_ok = touches >= SR_MIN_TOUCHES
                else:
                    sr_ok = False

        pair_state["structure_cache"] = {
            "bos": list(bos) if bos else None,
            "zones": zones,
            "liquidity_ok": liquidity_ok,
            "sr_ok": sr_ok,
            "bias_at_fetch": bias,
            "fetched_at": now_iso,
        }

    if bos:
        bos_level, pullback_zone, bos_index = bos
        current_setup = pair_state.get("setup")
        is_new_bos = not current_setup or current_setup.get("bos_level") != bos_level
        if is_new_bos:
            new_setup = {
                "bos_level": bos_level,
                "pullback_zone": pullback_zone,
                "confirmed": False,
            }
            if ENTRY_MODE == "structure":
                new_setup["zones"] = zones
                new_setup["liquidity_ok"] = liquidity_ok
                new_setup["sr_ok"] = sr_ok
            pair_state["setup"] = new_setup
            print(f"[{pair}] {bias} {TF_STRUCTURE} structure break at {bos_level:.5f}. Watching {TF_ENTRY} for confirmation.")

    setup = pair_state.get("setup")
    state[pair] = pair_state

    if not setup:
        print(f"[{pair}] No active setup.")
        return

    if setup.get("confirmed"):
        check_hold_time_nudges(pair, setup)
        print(f"[{pair}] Setup already confirmed — hold-time check done.")
        return

    if ENTRY_MODE == "structure":
        if not setup.get("zones"):
            print(f"[{pair}] No order block / supply-demand zone found for this break — skipping.")
            return
        if not setup.get("liquidity_ok", True):
            print(f"[{pair}] No liquidity sweep detected before the break — skipping (SMC confluence not met).")
            return
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
        confirmation = check_smc_confirmation(
            times5, opens5, highs5, lows5, closes5, bias, setup["zones"], SESSION_START_UTC, SESSION_END_UTC)
    elif ENTRY_MODE == "retest_or_pullback":
        # Whichever fires first counts — checked in this order each run.
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

    decimals = 3 if "JPY" in pair else (2 if "XAU" in pair else 5)
    label = f"[{STRATEGY_LABEL}] " if STRATEGY_LABEL else ""
    emoji = "🟢" if signal == "BUY" else "🔴"
    mode_desc = {
        "retest": "breakout + retest",
        "structure": f"{confirmation.get('zone_type', 'order_block')} + engulfing/rejection (session-filtered)",
        "retest_or_pullback": f"retest+pullback mode ({confirmation.get('trigger')} fired)",
        "pullback": "deep pullback (50-79% retrace)" if SWING_ENTRY_MODE else "pullback",
    }.get(ENTRY_MODE, ENTRY_MODE)
    tp_lines = "\n".join(
        f"TP{idx} ({m:g}R): `{tp:.{decimals}f}`" for idx, (m, tp) in enumerate(zip(TP_MULTIPLES, tps), start=1)
    )
    msg = (
        f"{emoji} *{label}{signal} — {pair}*\n"
        f"Bias: 4H {bias} | Structure: {TF_STRUCTURE} BOS {setup['bos_level']:.{decimals}f} | Trigger: 5M {mode_desc}\n"
        f"Entry: `{entry:.{decimals}f}`\n"
        f"SL: `{sl:.{decimals}f}`  (R = {r:.{decimals}f})\n"
        f"{tp_lines}\n"
        f"Bar: {times5[-1]} ({TF_ENTRY})"
    )
    if CHART_ENABLED:
        try:
            chart_path = generate_chart(pair, opens5, highs5, lows5, closes5, bias, signal, entry, sl, tps, CHART_CANDLES)
            send_telegram_photo(chart_path, msg)
            os.remove(chart_path)
        except Exception as e:
            print(f"[{pair}] Chart send failed ({e}) — falling back to text alert.")
            send_telegram(msg)
    else:
        send_telegram(msg)
    print(f"Entry alert sent for {pair}.")

    if AUTO_TRADE_ENABLED:
        filled, detail = place_demo_order(pair, signal, sl, tps[0])
        status = "✅ Demo order placed" if filled else "⚠️ Demo order NOT placed"
        send_telegram(f"{status} — {pair}\n{detail}\n(Only TP1 is set on the order — TP2-TP{len(tps)} must be managed manually.)")
        print(f"Demo trade [{pair}]: {status} — {detail}")

    setup["confirmed"] = True
    setup["last_entry_bar_time"] = times5[-1]
    if HOLD_TIME_NUDGES_ENABLED:
        setup["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        setup["notified_thresholds"] = []
    pair_state["setup"] = setup
    state[pair] = pair_state


def main():
    if not is_forex_market_open():
        print("Forex market is closed (weekend) — skipping this run to avoid false signals on stale data.")
        return

    state = load_state()
    any_errors = False

    for i, pair in enumerate(PAIRS):
        if i > 0:
            time.sleep(API_CALL_SLEEP)
        try:
            process_pair(pair, state)
        except Exception as e:
            print(f"ERROR [{pair}]: {e}", file=sys.stderr)
            any_errors = True

    save_state(state)

    if any_errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
