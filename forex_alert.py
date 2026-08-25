"""
Multi-timeframe price-action forex signal bot.

This one script drives THREE separate strategies, selected by ENTRY_MODE
(each gets its own workflow file):

  - 4H  : trend bias, from swing-high/swing-low structure
          (higher-high + higher-low = bullish, lower-high + lower-low = bearish)
  - 15M : structure break (BOS) in the direction of the 4H bias
  - 5M  : entry confirmation method, per ENTRY_MODE:

    STRUCTURE  (ENTRY_MODE=structure): SMC-style. 1H structure break +
    order block (last opposite-colored candle before the impulse), then
    a 5M engulfing candle or rejection wick inside that order block,
    restricted to the London/NY session window. SL anchors to the order
    block edge. (Liquidity-pool/equal-highs detection is not
    implemented — everything else in the "top-down ladder" playbook is.)

    RETEST     (ENTRY_MODE=retest): breakout + retest — price must come
    back and touch the exact broken 15M level, then close back beyond
    it in the trend direction (rejection at the level).

    PULLBACK   (ENTRY_MODE=pullback): pure price-action pullback — a 5M
    swing pivot forms, then price breaks back through it in the trend
    direction. SWING_ENTRY_MODE=true additionally requires a deep
    50-79% retracement of the breakout leg before confirming.

Sends BUY/SELL alerts to Telegram with an entry price, a structure-based
stop loss, and TP1-TP5 (1R through 5R by default, configurable via
TP_MULTIPLES). No fixed time stop — this is meant to run intraday/swing style.

Optionally places a demo MT5 order via MetaApi using SL + TP1 only
(MT5 orders carry a single TP field — TP2/TP3 must be managed manually,
e.g. partial closes or manual trailing).

Run on a schedule (recommended: every 5 minutes, matching the entry
timeframe) via GitHub Actions — see check-signal.yml.

State (per-pair bias, active structure break, whether it's already been
confirmed/alerted, plus cached 4H/structure results) is kept in
state.json so the same setup doesn't re-trigger a Telegram message on
every run, and so slower timeframes aren't re-fetched every cycle.

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

# Entry confirmation method on the 5M chart, applied after a 15M BOS:
#   "retest"   - breakout + retest: price must come back and touch the
#                exact broken 15M level, then close back beyond it in the
#                trend direction (rejection at the level). Used by Intraday.
#   "pullback" - pure price-action pullback: a 5M swing pivot forms, then
#                price breaks back through it in the trend direction.
#                Used by Swing (with SWING_ENTRY_MODE for retracement depth).
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

# For ENTRY_MODE=structure only (order block + session filter):
OB_LOOKBACK = int(os.environ.get("OB_LOOKBACK", "15"))
REJECTION_WICK_RATIO = float(os.environ.get("REJECTION_WICK_RATIO", "0.5"))
# London/NY combined session window, UTC hours (default ~07:00-21:00 UTC).
SESSION_START_UTC = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.environ.get("SESSION_END_UTC", "21"))

# For ENTRY_MODE=pullback only:
# false = first valid pullback confirms; true = requires a deeper 50-79%
# retracement of the breakout leg before confirming (used by Swing).
SWING_ENTRY_MODE = os.environ.get("SWING_ENTRY_MODE", "false").lower() == "true"
SWING_RETRACE_MIN = float(os.environ.get("SWING_RETRACE_MIN", "0.5"))
SWING_RETRACE_MAX = float(os.environ.get("SWING_RETRACE_MAX", "0.79"))

STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "")
STATE_FILENAME = os.environ.get("STATE_FILENAME", "state.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), STATE_FILENAME)

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


def check_structure_break(highs, lows, closes, bias):
    """Structure timeframe: has price broken the most recent relevant
    swing in the direction of `bias`? Returns
    (bos_level, pullback_zone_price, bos_swing_index) or None.
    pullback_zone_price is the prior opposite swing — pullbacks should not
    trade back beyond it without invalidating the setup. bos_swing_index
    is the bar index of the broken swing, used to locate the order block."""
    swings = find_swings(highs, lows, SWING_LOOKBACK)
    n = len(closes)
    last_close = closes[n - 1]

    if bias == "bullish":
        level_swing = last_swing_before(swings, "high", n - 1)
        if not level_swing:
            return None
        if last_close > level_swing["price"]:
            anchor = last_swing_before(swings, "low", level_swing["i"])
            anchor_price = anchor["price"] if anchor else min(
                lows[max(0, level_swing["i"] - 10):level_swing["i"]] or [lows[0]])
            return level_swing["price"], anchor_price, level_swing["i"]
    else:
        level_swing = last_swing_before(swings, "low", n - 1)
        if not level_swing:
            return None
        if last_close < level_swing["price"]:
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


# ---------------- SMC concepts: order block + session-filtered entry ----------------
# Used by ENTRY_MODE=structure. Mirrors: 4H bias -> 1H structure break +
# order block -> 5M engulfing/rejection confirmation inside that order
# block, during London/NY session hours. Liquidity-pool (equal highs/lows)
# detection is intentionally NOT implemented — it needs more heuristics to
# do reliably, so it's left out rather than faked.

def find_order_block(opens, highs, lows, closes, bias, before_index, lookback=15):
    """The order block is the last opposite-colored candle before the
    impulsive move that broke structure — for a bullish break, the last
    bearish (red) candle before the up-move; for bearish, the last
    bullish (green) candle before the down-move."""
    start = max(0, before_index - lookback)
    for i in range(before_index - 1, start - 1, -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]
        if bias == "bullish" and is_bearish:
            return {"high": highs[i], "low": lows[i]}
        if bias == "bearish" and is_bullish:
            return {"high": highs[i], "low": lows[i]}
    return None


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


def check_smc_confirmation(times, opens, highs, lows, closes, bias, ob, session_start, session_end):
    """5M: price trading inside the 1H order block, with either an
    engulfing candle or a rejection wick in the trend direction, during
    the configured session window."""
    n = len(closes)
    i = n - 1
    if not in_session(times[i], session_start, session_end):
        return None

    zone_low, zone_high = ob["low"], ob["high"]
    price_in_zone = lows[i] <= zone_high and highs[i] >= zone_low
    if not price_in_zone:
        return None

    if not (is_engulfing(opens, closes, bias, i) or
            has_rejection_wick(opens, highs, lows, closes, bias, i, zone_low, zone_high)):
        return None

    entry = closes[i]
    sl_anchor = zone_low if bias == "bullish" else zone_high
    return {"entry": entry, "sl_anchor": sl_anchor}


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
        ob = structure_cache.get("ob")
    else:
        times15, opens15, highs15, lows15, closes15 = fetch_series(pair, TF_STRUCTURE, outputsize=150)
        time.sleep(API_CALL_SLEEP)
        bos = check_structure_break(highs15, lows15, closes15, bias)
        ob = None
        if bos and ENTRY_MODE == "structure":
            ob = find_order_block(opens15, highs15, lows15, closes15, bias, bos[2], OB_LOOKBACK)
        pair_state["structure_cache"] = {
            "bos": list(bos) if bos else None,
            "ob": ob,
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
            if ENTRY_MODE == "structure" and ob:
                new_setup["ob"] = ob
            pair_state["setup"] = new_setup
            print(f"[{pair}] {bias} {TF_STRUCTURE} structure break at {bos_level:.5f}. Watching {TF_ENTRY} for confirmation.")

    setup = pair_state.get("setup")
    state[pair] = pair_state

    if not setup or setup.get("confirmed"):
        print(f"[{pair}] No active unconfirmed setup.")
        return

    if ENTRY_MODE == "structure" and not setup.get("ob"):
        print(f"[{pair}] No order block found for this break — skipping.")
        return

    # --- 5M entry: always fetched fresh, every run ---
    times5, opens5, highs5, lows5, closes5 = fetch_series(pair, TF_ENTRY, outputsize=150)
    time.sleep(API_CALL_SLEEP)
    a5 = atr(highs5, lows5, closes5, 14) or 0

    if ENTRY_MODE == "retest":
        confirmation = check_retest_confirmation(highs5, lows5, closes5, bias, setup["bos_level"], a5)
    elif ENTRY_MODE == "structure":
        confirmation = check_smc_confirmation(
            times5, opens5, highs5, lows5, closes5, bias, setup["ob"], SESSION_START_UTC, SESSION_END_UTC)
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

    decimals = 3 if "JPY" in pair else 5
    label = f"[{STRATEGY_LABEL}] " if STRATEGY_LABEL else ""
    emoji = "🟢" if signal == "BUY" else "🔴"
    mode_desc = {
        "retest": "breakout + retest",
        "structure": "order block + engulfing/rejection (session-filtered)",
        "retest_or_pullback": f"retest+pullback mode ({confirmation.get('trigger')} fired)",
        "pullback": "deep pullback (50-79% retrace)" if SWING_ENTRY_MODE else "pullback",
    }.get(ENTRY_MODE, ENTRY_MODE)
    tp_lines = "\n".join(
        f"TP{idx} ({m:g}R): `{tp:.{decimals}f}`" for idx, (m, tp) in enumerate(zip(TP_MULTIPLES, tps), start=1)
    )
    msg = (
        f"{emoji} *{label}{signal} — {pair}*\n"
        f"Bias: 4H {bias} | Structure: 15M BOS {setup['bos_level']:.{decimals}f} | Trigger: 5M {mode_desc}\n"
        f"Entry: `{entry:.{decimals}f}`\n"
        f"SL: `{sl:.{decimals}f}`  (R = {r:.{decimals}f})\n"
        f"{tp_lines}\n"
        f"No fixed time stop — manage by structure.\n"
        f"Bar: {times5[-1]} ({TF_ENTRY})"
    )
    send_telegram(msg)
    print(f"Entry alert sent for {pair}.")

    if AUTO_TRADE_ENABLED:
        filled, detail = place_demo_order(pair, signal, sl, tps[0])
        status = "✅ Demo order placed" if filled else "⚠️ Demo order NOT placed"
        send_telegram(f"{status} — {pair}\n{detail}\n(Only TP1 is set on the order — TP2-TP{len(tps)} must be managed manually.)")
        print(f"Demo trade [{pair}]: {status} — {detail}")

    setup["confirmed"] = True
    setup["last_entry_bar_time"] = times5[-1]
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
