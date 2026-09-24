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
    conviction booster (see classify_conviction) rather than required —
    most genuine breaks don't have a textbook equal-highs/lows pool
    sitting right before them, so gating on it starved the setup of
    signals. Candidate zones (order blocks + supply/demand) persist
    across runs in state.json with mitigation tracking — a zone stops
    being tradeable once price closes fully through it, rather than
    being rebuilt from scratch on every new break. A mitigated order
    block is also promoted into a "breaker block" — the same price
    range, flipped to the opposite direction — since a failed OB often
    acts as support/resistance in the new direction on a later retest
    (see update_order_block_mitigation / active_breaker_zones_for).
    Order-block candidates also require a genuine impulsive move to
    have followed them (OB_MIN_MOVE_ATR_MULT) — otherwise a random
    opposite-colored candle sitting in a choppy, non-impulsive stretch
    could be mistaken for a real order block (see find_order_blocks).
    Each break is tagged CHoCH (reverses the prior bias) or BOS
    (continues it). Entry confirms on a 5M engulfing candle, rejection
    wick, or fresh fair value gap inside any active zone (order block,
    supply/demand, or breaker), restricted to the London/NY session
    window. Each confirmed signal gets a condition label (e.g.
    "OB+CHoCH+FVG+LIQ+SR+BRK") and a conviction score that sets its
    hold-duration class (day / day_swing / swing), which in turn selects
    which single hold-time threshold applies to it.

    If every persisted zone for the current bias has been mitigated
    (price closed fully through it) but the underlying structure break
    is still valid, zones are re-derived immediately against the
    current break rather than waiting for the next natural
    STRUCTURE_CACHE_MINUTES refresh — see the "re-derive" block inside
    process_pair. This costs one extra Twelve Data call, only on runs
    where zones come back empty.

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
implemented is an optional hold-time *nudge*: once a confirmed signal
has been open longer than the single threshold matching its hold-
duration class (DAY_MAX_HOLD_HOURS / DAY_SWING_MAX_HOLD_HOURS /
SWING_MAX_HOLD_HOURS — any subset can be set; unset ones simply never
fire for that class), one Telegram reminder goes out so a forgotten
trade doesn't run indefinitely unnoticed. Non-structure modes (retest,
pullback) don't compute a conviction score, so they default to the
"day" threshold. This is a reminder based on wall-clock time since the
alert, not a real position-aware time stop.

Also sends a separate "Market Update" narrative post (matching the
📌 MARKET UPDATE / 🔥 TRADING PLAN style used by public gold/forex
channels) whenever a fresh structure break fires on TF_STRUCTURE. It
describes where price sits relative to the nearest liquidity zone and
what a sweep + rejection there would imply — it is NOT a trade signal,
just a structure-context post, and is independent of ENTRY_MODE and of
whether a 5M entry ever confirms. Toggle with MARKET_UPDATE_ENABLED.

Optionally places a demo MT5 order via MetaApi using SL + TP1 only
(MT5 orders carry a single TP field — TP2/TP3 must be managed manually,
e.g. partial closes or manual trailing).

Run on a schedule (recommended: every 5 minutes, matching the entry
timeframe) via GitHub Actions — see check-signal.yml.

State (per-pair bias, active structure break/zones, persisted order
blocks + breaker blocks with mitigation status, whether it's already
been confirmed/alerted, hold-time nudge history, plus cached 4H/
structure results) is kept in state.json so the same setup doesn't
re-trigger a Telegram message on every run, and so slower timeframes
aren't re-fetched every cycle.

API USAGE: with caching, only the 5M entry candle is fetched every run —
4H is cached for TREND_CACHE_MINUTES, structure for
STRUCTURE_CACHE_MINUTES. This is what makes a 5-minute cron viable on
Twelve Data's free tier (8 req/min, 800/day), but only for a small
number of pairs — 4 pairs x 2 workflows still won't fit even with
caching, since the 5M fetch alone is a hard floor. Keep FX_PAIRS short
per workflow if running on a 5-min schedule. The zone re-derive path
(structure mode only, only on empty-zone runs) adds up to one more
call, so watch quota if it fires often.
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

# Second, independently-toggleable session window — Sydney+Tokyo/Asian
# combined (~21:00-09:00 UTC, wraps midnight; the two overlap so they
# form one continuous block). Off by default since, together with
# London/NY above, enabling this covers essentially the full 24h day.
SESSION2_ENABLED = os.environ.get("SESSION2_ENABLED", "false").lower() == "true"
SESSION2_START_UTC = int(os.environ.get("SESSION2_START_UTC", "21"))
SESSION2_END_UTC = int(os.environ.get("SESSION2_END_UTC", "9"))

# For ENTRY_MODE=structure only — how many unmitigated order-block zones
# (plus one supply/demand zone, if found) to keep as live candidates,
# per direction (persisted across runs — see sync_order_blocks). The
# same cap is applied to breaker blocks per direction.
OB_MAX_ZONES = int(os.environ.get("OB_MAX_ZONES", "3"))

# For ENTRY_MODE=structure only — order-block quality filter. A
# candidate OB candle must be followed (before the break) by a move of
# at least this many ATRs in the trend direction, so a random opposite-
# colored candle sitting in a choppy range isn't mistaken for a real
# order block (i.e. a candle an impulsive leg actually originated from).
# Set to 0 to disable and accept any opposite-colored candle regardless
# of what happened afterward.
OB_MIN_MOVE_ATR_MULT = float(os.environ.get("OB_MIN_MOVE_ATR_MULT", "1.0"))

# For ENTRY_MODE=structure only — liquidity pool / sweep detection.
# Two or more swing highs (or lows) within this ATR-multiple tolerance of
# each other count as one "equal highs/lows" pool. Set LIQUIDITY_LOOKBACK
# to 0 to disable sweep detection entirely. NOTE: a detected sweep now
# only boosts conviction score (see classify_conviction) — it is not a
# hard requirement to enter (see change log at bottom of this section).
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

# ---- Market Update posts — a narrative structure-context message (the
# "📌 MARKET UPDATE / 🔥 TRADING PLAN" style used by public gold/forex
# channels), separate from the BUY/SELL trade alerts above. Fires once
# per fresh structure break (BOS/CHoCH) on TF_STRUCTURE, independent of
# ENTRY_MODE and of whether a 5M entry ever confirms. Not a trade
# signal — no entry/SL/TP, just "here's where price sits vs. the nearest
# liquidity zone and what a sweep+rejection there would imply." ----
MARKET_UPDATE_ENABLED = os.environ.get("MARKET_UPDATE_ENABLED", "true").lower() == "true"
MARKET_UPDATE_LABEL = os.environ.get("MARKET_UPDATE_LABEL", STRATEGY_LABEL or "Market")
MARKET_UPDATE_TF_LABEL = os.environ.get("MARKET_UPDATE_TF_LABEL", TF_STRUCTURE.upper())
# Low/high ATR multiples off the liquidity zone used to project the
# "could recover/drop toward X-Y" target range in the narrative.
MARKET_UPDATE_TARGET_ATR_MULTIPLES = tuple(
    float(x) for x in os.environ.get("MARKET_UPDATE_TARGET_ATR_MULTIPLES", "1.5,4.5").split(",") if x.strip()
)
# Minimum distance (in ATR multiples of the structure timeframe) the new
# bos_level must sit beyond the last *posted* Market Update's bos_level,
# in the same bias direction, before another update is sent. Prevents
# repeated, near-identical posts during a sustained trend where a fresh
# swing point forms every cache refresh but represents the same ongoing
# move rather than a meaningfully new development. A bias flip (genuine
# reversal) always posts regardless of this threshold. Set to 0 to
# disable and post on every new BOS/CHoCH as before.
MARKET_UPDATE_MIN_MOVE_ATR_MULT = float(os.environ.get("MARKET_UPDATE_MIN_MOVE_ATR_MULT", "1.0"))

# ---- hold-time nudges (any subset can be set; unset class = disabled for
# that class only). For ENTRY_MODE=structure, the class used is the
# conviction-based duration_class computed at confirmation time (day /
# day_swing / swing — see classify_conviction). Other modes default to
# "day". ----

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
# 1H displacement break, scored (not gated) by a prior liquidity sweep ->
# order block / supply-demand / breaker zones, optionally filtered by
# S/R confluence -> 5M engulfing/rejection/FVG confirmation inside a
# zone, during London/NY session hours.

def find_order_blocks(opens, highs, lows, closes, bias, before_index, lookback=15, max_zones=3,
                       atr_val=None, min_move_atr_mult=None):
    """Up to `max_zones` order-block candidates before the break — each
    is the last opposite-colored candle before an impulsive leg within
    this lookback window (for a bullish break: the last bearish candle
    before an up-move; for bearish: the last bullish candle before a
    down-move). Ordered nearest-to-the-break first.

    If `min_move_atr_mult` and `atr_val` are given, a candidate candle
    only counts if price actually moved at least `min_move_atr_mult` *
    ATR away from its close, in the trend direction, at some point
    between it and `before_index` — i.e. an impulsive leg genuinely
    originated there. Without this, any opposite-colored candle in the
    lookback window qualifies regardless of what happened afterward,
    which can flag a random candle from a choppy, non-impulsive stretch
    as an "order block." Leave both as None to skip the check (old
    behavior)."""
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
                    continue  # no real impulse followed this candle — skip it
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
    playbook rewards with higher conviction (see classify_conviction) —
    it is not required to enter."""
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
    """Human-readable trailing swing sequence for the market-update
    narrative. If highs and lows agree on direction, describes it as a
    clean trend (e.g. 'Lower Highs, Lower Lows'). If they disagree (a
    contracting or expanding range rather than a real trend), says so
    explicitly instead of implying a directional story that isn't
    there — this used to silently print e.g. 'Lower Highs, Higher Lows'
    next to a bearish-BOS narrative, describing a range as if it were a
    trend."""
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
    """Liquidity zone price for the market-update narrative: the most
    recent equal-highs/lows pool on the side price is approaching (the
    side opposite the trend — that's what a continuation move is heading
    toward to sweep), or the latest swing point on that side if no
    clustered pool exists."""
    side = "low" if bias == "bearish" else "high"
    side_pools = [p for p in pools if p["kind"] == side]
    if side_pools:
        return max(side_pools, key=lambda p: p["last_i"])["price"]
    matching = [s for s in swings if s["kind"] == side]
    if matching:
        return matching[-1]["price"]
    return fallback_price


def build_market_update_message(pair, bias, structure_seq, displacement_hit,
                                 liquidity_zone, atr_val, decimals, is_choch=False):
    """Narrative-style structure summary in the '📌 MARKET UPDATE / 🔥
    TRADING PLAN' format used by public gold/forex channels — separate
    from the BUY/SELL trade alert. Describes where price sits relative
    to the nearest liquidity zone and what a sweep + rejection there
    would imply, rather than a specific entry/SL/TP.

    `is_choch` now controls whether the break is described as a CHoCH
    (reversal) or a BOS (continuation) — previously this always said
    "BOS" regardless of the actual break type, so a genuine reversal was
    misreported as trend continuation."""
    break_word = "CHoCH" if is_choch else "BOS"
    bos_word = f"bearish {break_word}" if bias == "bearish" else f"bullish {break_word}"
    disp_word = f"clear {bias} displacement" if displacement_hit else f"a mild {bias} push (no strong displacement)"
    rejection_word = "bullish rejection" if bias == "bearish" else "bearish rejection"
    move_word = "recover toward" if bias == "bearish" else "drop toward"

    lo_mult = MARKET_UPDATE_TARGET_ATR_MULTIPLES[0]
    hi_mult = MARKET_UPDATE_TARGET_ATR_MULTIPLES[-1]
    atr_val = atr_val or 0
    if bias == "bearish":
        target_lo = liquidity_zone + lo_mult * atr_val
        target_hi = liquidity_zone + hi_mult * atr_val
    else:
        target_lo = liquidity_zone - hi_mult * atr_val
        target_hi = liquidity_zone - lo_mult * atr_val

    today = datetime.now(timezone.utc).strftime("%B %d").upper()
    display_pair = pair.replace("/", "")

    return (
        f"📌 MARKET UPDATE – {today}\n\n"
        f"{MARKET_UPDATE_LABEL}\n\n"
        f"— {display_pair} / {MARKET_UPDATE_TF_LABEL} —\n\n"
        f"🔥 TRADING PLAN – STRUCTURE UPDATE\n\n"
        f"{display_pair} is testing the liquidity zone around {liquidity_zone:.{decimals}f} "
        f"after a sequence of {structure_seq}, {bos_word}, and {disp_word}. "
        f"If a liquidity sweep occurs with {rejection_word}, price could {move_word} "
        f"{target_lo:.{decimals}f}–{target_hi:.{decimals}f}."
    )


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


def detect_fvg(opens, highs, lows, closes, i):
    """3-candle fair value gap ending at candle i. Bull: candle i's low
    sits above candle i-2's high (an un-retraced gap up), with the
    middle candle bullish. Bear is the mirror image. Used as a third,
    independent entry trigger alongside engulfing/rejection."""
    if i < 2:
        return None
    if lows[i] > highs[i - 2] and closes[i - 1] > opens[i - 1]:
        return "bull"
    if highs[i] < lows[i - 2] and closes[i - 1] < opens[i - 1]:
        return "bear"
    return None


def in_session(iso_time, start_hour, end_hour):
    """Single window check (UTC hours). fetch_series requests
    timezone=UTC explicitly, so iso_time is guaranteed to be UTC here."""
    try:
        hour = int(iso_time[11:13])
    except (IndexError, ValueError):
        return True  # fail open rather than silently blocking every trade
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour  # wraps past midnight


def in_any_session(iso_time):
    """True if iso_time falls in the London/NY window, OR (when enabled)
    the Sydney/Asian window. Used everywhere the old single-window
    in_session(times[i], SESSION_START_UTC, SESSION_END_UTC) call used
    to be."""
    if in_session(iso_time, SESSION_START_UTC, SESSION_END_UTC):
        return True
    if SESSION2_ENABLED and in_session(iso_time, SESSION2_START_UTC, SESSION2_END_UTC):
        return True
    return False


def check_smc_confirmation(times, opens, highs, lows, closes, bias, zones, session_start, session_end):
    """5M: price trading inside any candidate zone (order block, supply/
    demand, or breaker), with an engulfing candle, a rejection wick, or a
    fresh fair value gap in the trend direction, during the configured
    session window. Zones are checked nearest-to-the-break first; the
    first one that matches wins. Returns confirmation info plus the
    individual trigger flags (used downstream for the condition label
    and conviction score)."""
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
        sl_anchor = zone_low if bias == "bullish" else zone_high
        return {
            "entry": entry,
            "sl_anchor": sl_anchor,
            "zone_type": zone.get("type", "order_block"),
            "confirmations": {"engulfing": engulf, "rejection": rej, "fvg": fvg_hit},
        }
    return None


# ---------------- persistent order-block / breaker-block tracking (ENTRY_MODE=structure) ----------------
# Zones survive across runs in state.json (pair_state["order_blocks"] and
# pair_state["breaker_blocks"]), tagged with direction and whether they
# came from a CHoCH or a BOS break, and are deactivated once price
# closes fully through them (mitigated) rather than being rebuilt from
# scratch on every new break.
#
# A breaker block is a former order block that FAILED — price closed
# fully through it — so instead of just discarding it, the same price
# range is promoted into a new zone in the OPPOSITE direction (a broken
# bullish OB flips to act as resistance on a later bearish leg, and vice
# versa). This mirrors the real SMC idea that a failed OB often becomes
# a stronger reaction level than a fresh one, since it's where trapped
# opposite-side orders sit.

def update_order_block_mitigation(order_blocks, current_price, breaker_blocks=None):
    """Deactivate any persisted order block price has fully closed
    through — it's been mitigated and is no longer a valid zone in its
    original direction. If `breaker_blocks` is passed, the same price
    range is also promoted into that list as a breaker block, flipped to
    the opposite direction."""
    for ob in order_blocks:
        if not ob.get("active", True):
            continue
        if ob["direction"] == "bullish" and current_price < ob["bot"]:
            ob["active"] = False
            if breaker_blocks is not None:
                breaker_blocks.append({
                    "top": ob["top"], "bot": ob["bot"], "type": "breaker_block",
                    "direction": "bearish", "active": True, "is_choch": False,
                })
        elif ob["direction"] == "bearish" and current_price > ob["top"]:
            ob["active"] = False
            if breaker_blocks is not None:
                breaker_blocks.append({
                    "top": ob["top"], "bot": ob["bot"], "type": "breaker_block",
                    "direction": "bullish", "active": True, "is_choch": False,
                })


def update_breaker_mitigation(breaker_blocks, current_price):
    """A breaker block gets invalidated the same way an order block
    does — if price closes back fully through it (in its flipped
    direction), it's no longer a valid zone either. Not re-flipped again
    (no breaker-of-a-breaker chaining) — it's just dropped."""
    for bb in breaker_blocks:
        if not bb.get("active", True):
            continue
        if bb["direction"] == "bullish" and current_price < bb["bot"]:
            bb["active"] = False
        elif bb["direction"] == "bearish" and current_price > bb["top"]:
            bb["active"] = False


def prune_order_blocks(order_blocks, max_zones):
    """Keep at most max_zones per direction, oldest dropped first. Used
    for both order_blocks and breaker_blocks (same shape: a list of
    dicts with a "direction" key)."""
    for direction in ("bullish", "bearish"):
        same_dir = [ob for ob in order_blocks if ob["direction"] == direction]
        while len(same_dir) > max_zones:
            order_blocks.remove(same_dir.pop(0))


def sync_order_blocks(pair_state, bias, new_zones, is_choch, max_zones):
    """Persist freshly-found order block/supply-demand zones into
    pair_state (tagged with direction + CHoCH/BOS), alongside any still-
    active zones from earlier breaks, then prune per direction."""
    obs = pair_state.setdefault("order_blocks", [])
    for z in new_zones:
        obs.append({
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
    """Same shape as active_zones_for, but reading pair_state's
    breaker_blocks list instead of order_blocks."""
    return [
        {"high": bb["top"], "low": bb["bot"], "type": bb["type"]}
        for bb in reversed(pair_state.get("breaker_blocks", []))
        if bb["direction"] == bias and bb.get("active", True)
    ]


def build_condition_label(is_choch, confirmations, sr_hit, sd_hit, breaker_hit=False):
    """Builds a label like 'OB+CHoCH+FVG+LIQ+SR+BRK' from everything that
    actually fired for this signal, in a fixed, readable order."""
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
    return "+".join(parts)


def classify_conviction(is_choch, confirmations, sr_hit, sd_hit, breaker_hit=False):
    """Conviction score -> hold-duration class ("day" / "day_swing" /
    "swing"), which selects the single hold-time threshold applied to
    this signal (see check_hold_time_nudges). CHoCH and displacement
    carry the most weight since they indicate a genuinely new
    directional push, not just a pullback within an existing range. A
    breaker-block entry (a failed zone flipping and holding on retest)
    gets the same weight as an S/R or supply/demand confluence — it's a
    meaningful confluence but not on its own a reason to expect a longer
    hold."""
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

    if score >= 5:
        return "swing", "High conviction (CHoCH/displacement + multiple confluences) — manage by structure."
    elif score >= 3:
        return "day_swing", "Moderate conviction — consider partial at 1-2R, trail the rest."
    else:
        return "day", "Lower conviction, single-confirmation setup — treat as intraday."


def classify_conviction_generic(is_choch, displacement_hit, trigger, deep_retrace):
    """Lighter-weight conviction scorer for the non-structure modes
    (retest / pullback / retest_or_pullback), which have no zones,
    liquidity sweep, or FVG to draw on. Same duration-class thresholds
    as classify_conviction so hold-time nudging works identically."""
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


def draw_zone_rect(ax, x_start, x_end, y_low, y_high, color="#ef535030", edge_color=None):
    """Shaded rectangle for an order block / supply-demand / liquidity zone."""
    import matplotlib.pyplot as plt
    height = y_high - y_low
    rect = plt.Rectangle((x_start, y_low), x_end - x_start, height,
                          facecolor=color, edgecolor=edge_color or "none",
                          linewidth=1, zorder=1)
    ax.add_patch(rect)


def draw_swing_label(ax, x, y, text, above=True, color="#111"):
    """Small text label at a swing point, e.g. 'Lower High' / 'Higher Low'.
    Offset is in screen points (not price units) so it stays a small,
    fixed visual distance from the point regardless of price scale."""
    offset_points = (0, 8) if above else (0, -8)
    ax.annotate(text, xy=(x, y), xycoords="data",
                xytext=offset_points, textcoords="offset points",
                fontsize=8, color=color, ha="center",
                va="bottom" if above else "top", clip_on=False)


def draw_break_marker(ax, x_break, level, x_end, label, color="#111"):
    """Dashed horizontal level line + BOS/CHoCH text tag at the break point."""
    ax.plot([x_break, x_end], [level, level], linestyle="--", linewidth=1,
            color=color, alpha=0.6, zorder=2)
    ax.annotate(label, xy=(x_break, level), xytext=(x_break, level),
                fontsize=8, color=color, ha="center", va="bottom")


def draw_target_box(ax, x, y, text, bg_color="#1b9e4b"):
    """Rounded price-callout box, like the green target boxes in SMC charts.
    clip_on=False so it still renders even if it falls outside the
    auto-scaled candle range (targets/liquidity levels often do)."""
    ax.annotate(
        text, xy=(x, y), fontsize=9, color="white", ha="center", va="center",
        bbox=dict(boxstyle="round,pad=0.35", fc=bg_color, ec="none"),
        zorder=5, clip_on=False, annotation_clip=False,
    )


def draw_direction_arrow(ax, x_start, y_start, x_end, y_end, color="#111"):
    """Projected price-path arrow (the zig-zag reversal arrow in the
    reference chart). annotation_clip=False so the arrow still draws
    even when its endpoint sits outside the candle-derived y-range."""
    ax.annotate(
        "", xy=(x_end, y_end), xytext=(x_start, y_start),
        arrowprops=dict(arrowstyle="->", color=color, linewidth=1.3, shrinkA=0, shrinkB=0),
        zorder=4, annotation_clip=False,
    )


def generate_chart(pair, opens, highs, lows, closes, bias, signal, entry, sl, tps,
                    num_candles=40, zone=None, break_info=None):
    """Candlestick chart of the last `num_candles` entry-timeframe bars,
    with entry/SL/TP levels drawn as horizontal lines. Requires
    matplotlib (imported lazily so it's only needed when charts are on).

    Optional `zone` = {"low": ..., "high": ...} draws the order-block /
    zone the entry triggered from as a shaded rectangle. Optional
    `break_info` = {"x": <candle index in this window>, "level": ...,
    "label": "BOS"/"CHoCH"} draws the structure-break line."""
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
        draw_break_marker(ax, break_info["x"], break_info["level"], count - 1,
                           break_info["label"])

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
    """Annotated SMC-style chart for the Market Update post, rendered on
    mplfinance's candlestick engine (real date-axis ticks, proper OHLC
    styling) rather than hand-drawn Rectangle candles. All annotations —
    swing labels, BOS/CHoCH line, liquidity zone box, projected path +
    target callouts — are still driven entirely by this script's own SMC
    computation (find_swings / check_structure_break / liquidity zone),
    not by anything mplfinance infers on its own.

    Requires the `mplfinance` and `pandas` packages (add both to the
    workflow's pip install step alongside matplotlib/requests)."""
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

    # mplfinance draws candles at integer x-positions 0..count-1
    # regardless of the datetime index (it only uses the index for the
    # tick-label text) — so the 0-based indices from find_swings /
    # check_structure_break line up directly with no conversion.

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

    # Widen the y-range so the projected path + target boxes (which can
    # sit above/below the candle range) are actually visible.
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
    Telegram once it crosses the single hold-time threshold matching its
    hold-duration class. For ENTRY_MODE=structure that class comes from
    classify_conviction (day / day_swing / swing); other modes don't
    compute a conviction score, so they default to "day". Only fires
    once per setup (tracked via setup["notified"], part of state.json).

    NOTE: this only measures wall-clock time since the alert was sent —
    it has no visibility into whether the trade is actually still open
    (that lives on the broker/MT5 side, or in the user's own tracking),
    so treat it as a "go check on this" reminder, not a real exit."""
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
        return  # that class's threshold isn't set — no nudge for it

    if elapsed_hours >= threshold_hours:
        label = f"[{STRATEGY_LABEL}] " if STRATEGY_LABEL else ""
        cls_label = duration_class.replace("_", "/").upper()
        send_telegram(f"⏰ {label}{pair} — [{cls_label}] {message}\nOpen for ~{elapsed_hours:.1f}h.")
        setup["notified"] = True


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

    prior_bias = pair_state.get("bias")
    bias_flipped = prior_bias is not None and prior_bias != bias
    if bias_flipped:
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
        displacement_hit = structure_cache.get("displacement_hit", False)
        atr_s = structure_cache.get("atr")
        structure_seq = structure_cache.get("structure_seq", "no clear swing sequence")
        liquidity_zone = structure_cache.get("liquidity_zone")
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
        displacement_hit = (
            bool(atr_s) and (highs_s[-1] - lows_s[-1]) >= DISPLACEMENT_ATR_MULT * atr_s
        ) if bos else False

        # Swings + liquidity pools on the structure timeframe — computed
        # once here regardless of ENTRY_MODE, both for the structure-mode
        # liquidity-sweep check below and for the market-update narrative.
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

            # Persist zones into the running order-block store (mitigation-
            # tracked, tagged CHoCH/BOS), promote any newly-mitigated OB
            # into a breaker block, then prune both lists.
            sync_order_blocks(pair_state, bias, zones, bias_flipped, OB_MAX_ZONES)
            breakers = pair_state.setdefault("breaker_blocks", [])
            update_order_block_mitigation(pair_state.get("order_blocks", []), closes_s[-1], breaker_blocks=breakers)
            update_breaker_mitigation(breakers, closes_s[-1])
            prune_order_blocks(breakers, OB_MAX_ZONES)

        pair_state["structure_cache"] = {
            "bos": list(bos) if bos else None,
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
        bos_level, pullback_zone, bos_index = bos
        current_setup = pair_state.get("setup")
        is_new_bos = not current_setup or current_setup.get("bos_level") != bos_level
        if is_new_bos:
            new_setup = {
                "bos_level": bos_level,
                "pullback_zone": pullback_zone,
                "confirmed": False,
                "is_choch": bias_flipped,
                "displacement_hit": displacement_hit,
            }
            if ENTRY_MODE == "structure":
                new_setup["zones"] = zones
                new_setup["liquidity_ok"] = liquidity_ok
                new_setup["sr_ok"] = sr_ok
                new_setup["is_choch"] = bias_flipped
            pair_state["setup"] = new_setup
            print(f"[{pair}] {bias} {TF_STRUCTURE} structure break at {bos_level:.5f}. Watching {TF_ENTRY} for confirmation.")

            # Fresh structure break -> narrative Market Update post
            # (separate from the trade alert; not gated by ENTRY_MODE or
            # by whether a 5M entry ever confirms). is_new_bos above only
            # checks "did bos_level literally change" — during a
            # sustained trend that fires on every fresh swing point even
            # if it's a trivial distance past the last one. The check
            # below adds "is this far enough to be worth another post":
            # a bias flip always posts (a genuine reversal is inherently
            # newsworthy); same-direction continuation only posts if the
            # new bos_level has moved at least MARKET_UPDATE_MIN_MOVE_ATR_MULT
            # x ATR beyond the last *posted* update's level.
            last_update = pair_state.get("last_market_update")
            should_post_update = True
            if last_update and last_update.get("bias") == bias and not bias_flipped:
                move_threshold = MARKET_UPDATE_MIN_MOVE_ATR_MULT * (atr_s or 0)
                distance = abs(bos_level - last_update.get("bos_level", bos_level))
                if move_threshold > 0 and distance < move_threshold:
                    should_post_update = False
                    print(f"[{pair}] New {bias} BOS only {distance:.5f} beyond last posted update "
                          f"(threshold {move_threshold:.5f}) — skipping Market Update, same trend continuing.")

            if MARKET_UPDATE_ENABLED and liquidity_zone is not None and should_post_update:
                decimals = 3 if "JPY" in pair else (2 if "XAU" in pair else 5)
                update_msg = build_market_update_message(
                    pair, bias, structure_seq, displacement_hit, liquidity_zone, atr_s, decimals,
                    is_choch=bias_flipped,
                )
                lo_mult = MARKET_UPDATE_TARGET_ATR_MULTIPLES[0]
                hi_mult = MARKET_UPDATE_TARGET_ATR_MULTIPLES[-1]
                atr_for_targets = atr_s or 0
                if bias == "bearish":
                    target_lo = liquidity_zone + lo_mult * atr_for_targets
                    target_hi = liquidity_zone + hi_mult * atr_for_targets
                else:
                    target_lo = liquidity_zone - hi_mult * atr_for_targets
                    target_hi = liquidity_zone - lo_mult * atr_for_targets
                try:
                    if not use_cached_structure:
                        chart_path = generate_market_update_chart(
                            pair, times_s, opens_s, highs_s, lows_s, closes_s, bias,
                            swings_s, bos_index, bos_level, bias_flipped,
                            liquidity_zone, atr_s, target_lo, target_hi,
                        )
                        send_telegram_photo(chart_path, update_msg)
                        os.remove(chart_path)
                    else:
                        send_telegram(update_msg)
                    print(f"[{pair}] Market update posted.")
                    pair_state["last_market_update"] = {"bias": bias, "bos_level": bos_level}
                except Exception as e:
                    print(f"[{pair}] Market update chart failed ({e}) — falling back to text.")
                    try:
                        send_telegram(update_msg)
                        pair_state["last_market_update"] = {"bias": bias, "bos_level": bos_level}
                    except Exception as e2:
                        print(f"[{pair}] Market update send failed: {e2}")

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
        if not setup.get("zones") and not pair_state.get("breaker_blocks"):
            print(f"[{pair}] No order block / supply-demand / breaker zone found for this break — skipping.")
            return
        # NOTE: liquidity sweep is intentionally NOT gated here anymore.
        # It used to hard-block entry when no sweep was detected, but a
        # genuine equal-highs/lows pool swept right before the break is
        # a fairly rare, specific pattern — gating on it starved the
        # setup of otherwise-valid signals for a week straight. It's
        # still detected (setup["liquidity_ok"]) and still feeds
        # classify_conviction() as a score booster below, so a sweep
        # still earns a longer expected hold and shows "LIQ" in the
        # condition label — it just no longer blocks entry on its own.
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
        # Re-check mitigation against the freshest (5M) close (order
        # blocks flip into breakers here too, and breakers get their own
        # mitigation check), then use whichever persisted zones for this
        # bias are still active, merging order blocks + breakers.
        breakers = pair_state.setdefault("breaker_blocks", [])
        update_order_block_mitigation(pair_state.get("order_blocks", []), closes5[-1], breaker_blocks=breakers)
        update_breaker_mitigation(breakers, closes5[-1])
        active_zones = active_zones_for(pair_state, bias) + active_breaker_zones_for(pair_state, bias)

        if not active_zones:
            # All persisted zones (order blocks and breakers) for this
            # bias have been mitigated. Rather than waiting up to
            # STRUCTURE_CACHE_MINUTES for the next natural refresh,
            # re-derive candidates right now against the current break
            # so a still-valid setup isn't stuck signal-less.
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
                    sync_order_blocks(pair_state, bias, fresh_zones, False, OB_MAX_ZONES)
                    update_order_block_mitigation(pair_state.get("order_blocks", []), closes5[-1], breaker_blocks=breakers)
                    update_breaker_mitigation(breakers, closes5[-1])
                    prune_order_blocks(breakers, OB_MAX_ZONES)
                    active_zones = active_zones_for(pair_state, bias) + active_breaker_zones_for(pair_state, bias)
                    print(f"[{pair}] Re-derived {len(fresh_zones)} fresh zone(s) after mitigation.")

        if not active_zones:
            print(f"[{pair}] No active order-block/zone/breaker remaining for this bias — skipping.")
            confirmation = None
        else:
            confirmation = check_smc_confirmation(
                times5, opens5, highs5, lows5, closes5, bias, active_zones, SESSION_START_UTC, SESSION_END_UTC)
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

    # --- structure mode only: condition label + conviction-based duration class ---
    if ENTRY_MODE == "structure":
        confs = confirmation.get("confirmations", {})
        full_confirmations = {
            "fvg": confs.get("fvg", False),
            "liquidity": setup.get("liquidity_ok", False),
            "displacement": True,  # structure break already required displacement to fire
            "rejection": confs.get("rejection", False),
        }
        sr_hit = SR_MIN_TOUCHES > 0 and setup.get("sr_ok", False)
        sd_hit = confirmation.get("zone_type") in ("demand_zone", "supply_zone")
        breaker_hit = confirmation.get("zone_type") == "breaker_block"
        is_choch = setup.get("is_choch", False)
        confirmation["condition_label"] = build_condition_label(is_choch, full_confirmations, sr_hit, sd_hit, breaker_hit)
        confirmation["duration_class"], confirmation["duration_note"] = classify_conviction(
            is_choch, full_confirmations, sr_hit, sd_hit, breaker_hit)
    else:
        is_choch = setup.get("is_choch", False)
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

    decimals = 3 if "JPY" in pair else (2 if "XAU" in pair else 5)
    label = f"[{STRATEGY_LABEL}] " if STRATEGY_LABEL else ""
    emoji = "🟢" if signal == "BUY" else "🔴"
    mode_desc = {
        "retest": "breakout + retest",
        "structure": f"{confirmation.get('zone_type', 'order_block')} + engulfing/rejection/FVG (session-filtered)",
        "retest_or_pullback": f"retest+pullback mode ({confirmation.get('trigger')} fired)",
        "pullback": "deep pullback (50-79% retrace)" if SWING_ENTRY_MODE else "pullback",
    }.get(ENTRY_MODE, ENTRY_MODE)

    setup_line = ""
    if confirmation.get("duration_class"):
        cls = confirmation.get("duration_class", "day").replace("_", "/").upper()
        setup_line = f"Setup: {confirmation.get('condition_label', '')} | Conviction: {cls} — {confirmation.get('duration_note', '')}\n"

    tp_lines = "\n".join(
        f"TP{idx} ({m:g}R): `{tp:.{decimals}f}`" for idx, (m, tp) in enumerate(zip(TP_MULTIPLES, tps), start=1)
    )
    msg = (
        f"{emoji} *{label}{signal} — {pair}*\n"
        f"Bias: 4H {bias} | Structure: {TF_STRUCTURE} BOS {setup['bos_level']:.{decimals}f} | Trigger: 5M {mode_desc}\n"
        f"{setup_line}"
        f"Entry: `{entry:.{decimals}f}`\n"
        f"SL: `{sl:.{decimals}f}`  (R = {r:.{decimals}f})\n"
        f"{tp_lines}\n"
        f"Bar: {times5[-1]} ({TF_ENTRY})"
    )
    if CHART_ENABLED:
        try:
            chart_zone = None
            chart_break_info = None
            if ENTRY_MODE == "structure":
                # Best-effort: the active zone the entry actually triggered
                # from (order block or breaker), and the original BOS/
                # CHoCH level, drawn onto the 5M signal chart the same way
                # as the market-update chart.
                az = active_zones_for(pair_state, bias) + active_breaker_zones_for(pair_state, bias)
                if az:
                    chart_zone = az[0]
                chart_break_info = {
                    "x": 0,
                    "level": setup["bos_level"],
                    "label": "CHoCH" if setup.get("is_choch") else "BOS",
                }
            chart_path = generate_chart(pair, opens5, highs5, lows5, closes5, bias, signal, entry, sl, tps,
                                         CHART_CANDLES, zone=chart_zone, break_info=chart_break_info)
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
    setup["duration_class"] = confirmation.get("duration_class", "day")
    setup["condition_label"] = confirmation.get("condition_label", "")
    if HOLD_TIME_NUDGES_ENABLED:
        setup["confirmed_at"] = datetime.now(timezone.utc).isoformat()
        setup["notified"] = False
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
