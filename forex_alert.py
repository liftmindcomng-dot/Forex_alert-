"""
Checks an EMA(12/26) crossover + RSI(14) filter on one or more forex pairs
using the Twelve Data API, and sends a Telegram message when a fresh BUY or
SELL signal appears. Designed to be run on a schedule (e.g. every 15
minutes) by GitHub Actions — see .github/workflows/check-signal.yml.

State (which bar we last alerted on, per pair) is kept in state.json so the
same crossover doesn't trigger a repeat message on every run.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timezone

# ---- config (env vars, set as GitHub Actions secrets/variables) ----
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# Comma-separated list, e.g. "EUR/USD,GBP/USD,USD/JPY"
PAIRS = [p.strip() for p in os.environ.get("FX_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD,XAU/USD").split(",") if p.strip()]
INTERVAL = os.environ.get("FX_INTERVAL", "15min")

# Strategy tuning — lets one script run as either a slower "swing" profile
# or a faster "scalp" profile, controlled entirely by workflow env vars.
EMA_FAST = int(os.environ.get("EMA_FAST", "12"))
EMA_SLOW = int(os.environ.get("EMA_SLOW", "26"))
RSI_OVERBOUGHT = float(os.environ.get("RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = float(os.environ.get("RSI_OVERSOLD", "30"))
ATR_SL_MULT = float(os.environ.get("ATR_SL_MULT", "1.5"))
ATR_TP_MULT = float(os.environ.get("ATR_TP_MULT", "3"))
ATR_BE_MULT = float(os.environ.get("ATR_BE_MULT", "1.0"))
STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "")  # e.g. "SCALP" or "SWING"

STATE_FILENAME = os.environ.get("STATE_FILENAME", "state.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), STATE_FILENAME)

# ---- broker symbol mapping ----
# Twelve Data uses "EUR/USD" style symbols; your MT5 broker (Exness) uses
# a suffixed style like "EURUSDm". Adjust SYMBOL_SUFFIX if your broker
# uses a different suffix (some use no suffix, some use ".m", etc.) —
# check your MT5 app's symbol list (Quotes tab) to confirm.
SYMBOL_SUFFIX = os.environ.get("BROKER_SYMBOL_SUFFIX", "m")


def to_broker_symbol(pair):
    """'EUR/USD' -> 'EURUSDm', 'XAU/USD' -> 'XAUUSDm', etc."""
    return pair.replace("/", "") + SYMBOL_SUFFIX

# ---- MetaApi (MT5) demo auto-trade — OPTIONAL, off by default ----
# IMPORTANT: unlike OANDA, MetaApi is broker-agnostic and works with any
# MT5 account — demo or live. This script has NO way to verify which
# type is linked. Safety here depends entirely on you making sure the
# MT5 account you connect in the MetaApi dashboard is a DEMO account.
AUTO_TRADE_ENABLED = os.environ.get("AUTO_TRADE_ENABLED", "false").lower() == "true"
METAAPI_TOKEN = os.environ.get("METAAPI_TOKEN", "")
METAAPI_ACCOUNT_ID = os.environ.get("METAAPI_ACCOUNT_ID", "")
TRADE_LOT_SIZE = float(os.environ.get("TRADE_LOT_SIZE", "0.01"))  # small demo size


def fetch_series(pair, interval, outputsize=100):
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": API_KEY,
    })
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")
    rows = list(reversed(data["values"]))  # oldest -> newest
    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    times = [r["datetime"] for r in rows]
    return times, highs, lows, closes


def ema(values, period):
    k = 2 / (period + 1)
    out = []
    prev = None
    for i, v in enumerate(values):
        prev = v if i == 0 else v * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values, period=14):
    out = [None] * len(values)
    if len(values) <= period:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = 100 - (100 / (1 + (100 if avg_loss == 0 else avg_gain / avg_loss)))
    for i in range(period + 1, len(values)):
        diff = values[i] - values[i - 1]
        gain, loss = max(diff, 0), max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rs = 100 if avg_loss == 0 else avg_gain / avg_loss
        out[i] = 100 - (100 / (1 + rs))
    return out


def atr(highs, lows, closes, period=14):
    """Average True Range — measures recent volatility, used to size SL/TP."""
    n = len(closes)
    true_ranges = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        true_ranges.append(tr)
    if len(true_ranges) < period:
        return None
    # Wilder's smoothing, same style as the RSI averaging above
    avg = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


def compute_signal(times, highs, lows, closes):
    e_fast, e_slow, r14 = ema(closes, EMA_FAST), ema(closes, EMA_SLOW), rsi(closes, 14)
    a14 = atr(highs, lows, closes, 14)
    n = len(closes)
    last_price, last_ef, last_es, last_rsi = closes[n-1], e_fast[n-1], e_slow[n-1], r14[n-1]
    prev_ef, prev_es = e_fast[n-2], e_slow[n-2]

    crossed_up = prev_ef <= prev_es and last_ef > last_es
    crossed_down = prev_ef >= prev_es and last_ef < last_es

    sl = tp = be = None
    if crossed_up and last_rsi < RSI_OVERBOUGHT:
        signal, reason = "BUY", (
            f"EMA{EMA_FAST} crossed above EMA{EMA_SLOW} with RSI at {last_rsi:.1f} "
            f"(below {RSI_OVERBOUGHT:.0f})."
        )
        if a14:
            sl = last_price - ATR_SL_MULT * a14
            tp = last_price + ATR_TP_MULT * a14
            be = last_price + ATR_BE_MULT * a14
    elif crossed_down and last_rsi > RSI_OVERSOLD:
        signal, reason = "SELL", (
            f"EMA{EMA_FAST} crossed below EMA{EMA_SLOW} with RSI at {last_rsi:.1f} "
            f"(above {RSI_OVERSOLD:.0f})."
        )
        if a14:
            sl = last_price + ATR_SL_MULT * a14
            tp = last_price - ATR_TP_MULT * a14
            be = last_price - ATR_BE_MULT * a14
    else:
        signal, reason = "HOLD", "No fresh crossover this bar."

    return {
        "bar_time": times[n-1],
        "signal": signal,
        "reason": reason,
        "price": last_price,
        "ema_fast": last_ef,
        "ema_slow": last_es,
        "rsi": last_rsi,
        "atr": a14,
        "sl": sl,
        "tp": tp,
        "be": be,
        "is_fresh_crossover": crossed_up or crossed_down,
    }


def load_state():
    """State is keyed by pair: {"EUR/USD": {"last_alert_bar_time": ...}, ...}"""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            data = json.load(f)
        # migrate old single-pair format if present
        if "last_alert_bar_time" in data:
            return {PAIRS[0]: {"last_alert_bar_time": data["last_alert_bar_time"]}}
        return data
    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


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


def place_demo_order(pair, signal, sl, tp):
    """
    Places a market order via MetaApi on whichever MT5 account is linked
    to METAAPI_ACCOUNT_ID. Returns (success: bool, message: str) — never
    raises, so one failed order never crashes the whole run.
    """
    if not (METAAPI_TOKEN and METAAPI_ACCOUNT_ID):
        return False, "MetaApi credentials not set — skipped."

    try:
        import asyncio
        from metaapi_cloud_sdk import MetaApi
    except ImportError:
        return False, "metaapi-cloud-sdk not installed."

    symbol = to_broker_symbol(pair)  # e.g. "EUR/USD" -> "EURUSDm"

    async def _place():
        api = MetaApi(METAAPI_TOKEN)
        account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)
        await account.wait_connected()
        connection = account.get_rpc_connection()
        await connection.connect()
        await connection.wait_synchronized()

        kwargs = {}
        if sl is not None:
            kwargs["stop_loss"] = sl
        if tp is not None:
            kwargs["take_profit"] = tp

        if signal == "BUY":
            result = await connection.create_market_buy_order(symbol, TRADE_LOT_SIZE, **kwargs)
        else:
            result = await connection.create_market_sell_order(symbol, TRADE_LOT_SIZE, **kwargs)
        return result

    try:
        result = asyncio.run(_place())
        return True, f"Order result: {result}"
    except Exception as e:
        return False, f"MetaApi order failed: {e}"


def is_forex_market_open():
    """
    Forex trades ~24hrs Mon-Fri, closed roughly Fri 22:00 UTC to Sun 22:00 UTC.
    This is a simple approximation, not exact broker hours, but enough to
    skip the weekend window where price feeds go stale/indicative and
    produce false crossover signals on noise instead of real movement.
    """
    now = datetime.now(timezone.utc)
    weekday = now.weekday()  # Monday=0 ... Sunday=6
    hour = now.hour

    if weekday == 5:  # Saturday — always closed
        return False
    if weekday == 4 and hour >= 22:  # Friday after 22:00 UTC
        return False
    if weekday == 6 and hour < 22:  # Sunday before 22:00 UTC
        return False
    return True


def main():
    if not is_forex_market_open():
        print("Forex market is closed (weekend) — skipping this run to avoid false signals on stale data.")
        return

    state = load_state()
    any_errors = False

    for i, pair in enumerate(PAIRS):
        if i > 0:
            time.sleep(8)  # stay under Twelve Data's free-tier rate limit (8 req/min)

        try:
            times, highs, lows, closes = fetch_series(pair, INTERVAL)
            result = compute_signal(times, highs, lows, closes)
        except Exception as e:
            print(f"ERROR [{pair}]: {e}", file=sys.stderr)
            any_errors = True
            continue

        print(f"[{pair} {INTERVAL}] bar={result['bar_time']} signal={result['signal']} "
              f"price={result['price']} rsi={result['rsi']:.1f}")

        pair_state = state.get(pair, {"last_alert_bar_time": None})
        already_alerted = pair_state.get("last_alert_bar_time") == result["bar_time"]

        if result["is_fresh_crossover"] and result["signal"] in ("BUY", "SELL") and not already_alerted:
            emoji = "🟢" if result["signal"] == "BUY" else "🔴"
            decimals = 3 if "JPY" in pair else 5
            label = f"[{STRATEGY_LABEL}] " if STRATEGY_LABEL else ""
            msg = (
                f"{emoji} *{label}{result['signal']} — {pair}*\n"
                f"Price: `{result['price']:.{decimals}f}`\n"
                f"RSI(14): `{result['rsi']:.1f}`\n"
            )
            if result["sl"] is not None:
                msg += (
                    f"SL: `{result['sl']:.{decimals}f}`\n"
                    f"TP: `{result['tp']:.{decimals}f}`\n"
                    f"Move to BE at: `{result['be']:.{decimals}f}`\n"
                )
            else:
                msg += "SL/TP: not enough history to calculate ATR yet.\n"
            msg += (
                f"{result['reason']}\n"
                f"Bar: {result['bar_time']} ({INTERVAL})"
            )
            send_telegram(msg)

            if AUTO_TRADE_ENABLED:
                filled, detail = place_demo_order(pair, result["signal"], result["sl"], result["tp"])
                status = "✅ Demo order placed" if filled else "⚠️ Demo order NOT placed"
                send_telegram(f"{status} — {pair}\n{detail}")
                print(f"Demo trade [{pair}]: {status} — {detail}")

            state[pair] = {"last_alert_bar_time": result["bar_time"]}
            print(f"Alert sent for {pair}.")
        else:
            state[pair] = pair_state
            print(f"No alert needed for {pair}.")

    save_state(state)

    if any_errors:
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
