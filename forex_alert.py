"""
Checks an EMA(12/26) crossover + RSI(14) filter on one or more forex pairs
using the Twelve Data API, and sends a Telegram message when a fresh BUY or
SELL signal appears on any of them. Designed to be run on a schedule (e.g.
every 15 minutes) by GitHub Actions — see .github/workflows/check-signal.yml.

State (which bar we last alerted on, per pair) is kept in state.json so the
same crossover doesn't trigger a repeat message on every run.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse

# ---- config (env vars, set as GitHub Actions secrets/variables) ----
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
# Comma-separated list, e.g. "EUR/USD,GBP/USD,USD/JPY"
PAIRS = [p.strip() for p in os.environ.get("FX_PAIRS", "EUR/USD,GBP/USD,USD/JPY,AUD/USD,XAU/USD").split(",") if p.strip()]
INTERVAL = os.environ.get("FX_INTERVAL", "15min")
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")


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
    e12, e26, r14 = ema(closes, 12), ema(closes, 26), rsi(closes, 14)
    a14 = atr(highs, lows, closes, 14)
    n = len(closes)
    last_price, last_e12, last_e26, last_rsi = closes[n-1], e12[n-1], e26[n-1], r14[n-1]
    prev_e12, prev_e26 = e12[n-2], e26[n-2]

    crossed_up = prev_e12 <= prev_e26 and last_e12 > last_e26
    crossed_down = prev_e12 >= prev_e26 and last_e12 < last_e26

    sl = tp = be = None
    if crossed_up and last_rsi < 70:
        signal, reason = "BUY", (
            f"EMA12 crossed above EMA26 with RSI at {last_rsi:.1f} (below 70)."
        )
        if a14:
            sl = last_price - 1.5 * a14
            tp = last_price + 3 * a14
            be = last_price + 1.0 * a14
    elif crossed_down and last_rsi > 30:
        signal, reason = "SELL", (
            f"EMA12 crossed below EMA26 with RSI at {last_rsi:.1f} (above 30)."
        )
        if a14:
            sl = last_price + 1.5 * a14
            tp = last_price - 3 * a14
            be = last_price - 1.0 * a14
    else:
        signal, reason = "HOLD", "No fresh crossover this bar."

    return {
        "bar_time": times[n-1],
        "signal": signal,
        "reason": reason,
        "price": last_price,
        "ema12": last_e12,
        "ema26": last_e26,
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


def main():
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
            msg = (
                f"{emoji} *{result['signal']} — {pair}*\n"
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
