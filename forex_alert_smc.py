"""
Smart Money Concepts (SMC/ICT) signal detector for XAU/USD (or any pair),
using the Twelve Data API, and sends a Telegram message when a fresh BUY
or SELL setup CONFIRMS on a freshly closed candle.

A setup requires, all confirmed as of the same closed candle or the
handful of candles immediately preceding it:

  BUY:
    1. Liquidity sweep BELOW a recent swing low (wick below, close back above)
    2. Bullish MSS/CHoCH — close breaks back above the swing high that
       formed the down-leg into the sweep
    3. A bullish FVG OR a bullish Order Block present in that same
       structural leg

  SELL: the mirror image (sweep above a swing high, bearish MSS/CHoCH,
  bearish FVG OR bearish Order Block)

No session filter — runs 24/5 alongside whatever market hours the pair
trades.
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
PAIRS = [p.strip() for p in os.environ.get("FX_PAIRS", "XAU/USD").split(",") if p.strip()]
INTERVAL = os.environ.get("FX_INTERVAL", "5min")

SWING_LEFT = int(os.environ.get("SWING_LEFT", "2"))
SWING_RIGHT = int(os.environ.get("SWING_RIGHT", "2"))
MAX_LEG_CANDLES = int(os.environ.get("MAX_LEG_CANDLES", "12"))
RR_RATIO = float(os.environ.get("RR_RATIO", "2.0"))
SL_BUFFER_ATR_MULT = float(os.environ.get("SL_BUFFER_ATR_MULT", "0.1"))
STRATEGY_LABEL = os.environ.get("STRATEGY_LABEL", "SMC")

STATE_FILENAME = os.environ.get("STATE_FILENAME", "state_smc.json")
STATE_FILE = os.path.join(os.path.dirname(__file__), STATE_FILENAME)


def fetch_series(pair, interval, outputsize=150):
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
    rows = list(reversed(data["values"]))
    opens = [float(r["open"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]
    closes = [float(r["close"]) for r in rows]
    times = [r["datetime"] for r in rows]
    return times, opens, highs, lows, closes


def atr(highs, lows, closes, period=14):
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
    avg = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


def find_swing_points(highs, lows, left, right):
    swing_highs, swing_lows = [], []
    n = len(highs)
    for i in range(left, n - right):
        window_h = highs[i - left:i + right + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            swing_highs.append((i, highs[i]))
        window_l = lows[i - left:i + right + 1]
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            swing_lows.append((i, lows[i]))
    return swing_highs, swing_lows


def last_before(points, idx):
    candidates = [p for p in points if p[0] < idx]
    return candidates[-1] if candidates else None


def detect_fvg_in_range(opens, highs, lows, closes, start, end, bullish):
    for i in range(max(start, 2), end + 1):
        if bullish and highs[i - 2] < lows[i]:
            return True
        if not bullish and lows[i - 2] > highs[i]:
            return True
    return False


def detect_order_block(opens, closes, impulse_start, impulse_end, bullish):
    for i in range(impulse_start, max(impulse_start - 6, -1), -1):
        is_red = closes[i] < opens[i]
        is_green = closes[i] > opens[i]
        if bullish and is_red:
            return i
        if not bullish and is_green:
            return i
    return None


def compute_signal(times, opens, highs, lows, closes):
    n = len(closes)
    last_i = n - 1
    a14 = atr(highs, lows, closes, 14)
    swing_highs, swing_lows = find_swing_points(highs, lows, SWING_LEFT, SWING_RIGHT)

    result_base = {
        "bar_time": times[last_i],
        "signal": "HOLD",
        "reason": "No confirmed setup this bar.",
        "price": closes[last_i],
        "atr": a14,
        "sl": None,
        "tp": None,
        "is_fresh_signal": False,
    }

    confirm_i = last_i

    recent_swing_low = last_before(swing_lows, confirm_i)
    if recent_swing_low:
        sweep_i = None
        for i in range(confirm_i, max(confirm_i - MAX_LEG_CANDLES, recent_swing_low[0]), -1):
            if lows[i] < recent_swing_low[1] and closes[i] > recent_swing_low[1]:
                sweep_i = i
                break
        if sweep_i is not None:
            leg_high = last_before(swing_highs, sweep_i)
            if leg_high:
                mss_level = leg_high[1]
                if closes[confirm_i] > mss_level:
                    has_fvg = detect_fvg_in_range(opens, highs, lows, closes, sweep_i, confirm_i, bullish=True)
                    ob_i = detect_order_block(opens, closes, sweep_i, confirm_i, bullish=True) if not has_fvg else None
                    if has_fvg or ob_i is not None:
                        entry = closes[confirm_i]
                        buffer = (a14 or 0) * SL_BUFFER_ATR_MULT
                        sl = lows[sweep_i] - buffer
                        risk = entry - sl
                        tp = entry + RR_RATIO * risk if risk > 0 else None
                        tag = "FVG" if has_fvg else "Order Block"
                        result_base.update({
                            "signal": "BUY",
                            "reason": (
                                f"Liquidity swept below {recent_swing_low[1]:.2f}, bullish MSS "
                                f"broke back above {mss_level:.2f}, confirmed by a bullish {tag}."
                            ),
                            "sl": sl,
                            "tp": tp,
                            "is_fresh_signal": True,
                        })
                        return result_base

    recent_swing_high = last_before(swing_highs, confirm_i)
    if recent_swing_high:
        sweep_i = None
        for i in range(confirm_i, max(confirm_i - MAX_LEG_CANDLES, recent_swing_high[0]), -1):
            if highs[i] > recent_swing_high[1] and closes[i] < recent_swing_high[1]:
                sweep_i = i
                break
        if sweep_i is not None:
            leg_low = last_before(swing_lows, sweep_i)
            if leg_low:
                mss_level = leg_low[1]
                if closes[confirm_i] < mss_level:
                    has_fvg = detect_fvg_in_range(opens, highs, lows, closes, sweep_i, confirm_i, bullish=False)
                    ob_i = detect_order_block(opens, closes, sweep_i, confirm_i, bullish=False) if not has_fvg else None
                    if has_fvg or ob_i is not None:
                        entry = closes[confirm_i]
                        buffer = (a14 or 0) * SL_BUFFER_ATR_MULT
                        sl = highs[sweep_i] + buffer
                        risk = sl - entry
                        tp = entry - RR_RATIO * risk if risk > 0 else None
                        tag = "FVG" if has_fvg else "Order Block"
                        result_base.update({
                            "signal": "SELL",
                            "reason": (
                                f"Liquidity swept above {recent_swing_high[1]:.2f}, bearish MSS "
                                f"broke back below {mss_level:.2f}, confirmed by a bearish {tag}."
                            ),
                            "sl": sl,
                            "tp": tp,
                            "is_fresh_signal": True,
                        })
                        return result_base

    return result_base


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
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
            time.sleep(8)

        try:
            times, opens, highs, lows, closes = fetch_series(pair, INTERVAL)
            result = compute_signal(times, opens, highs, lows, closes)
        except Exception as e:
            print(f"ERROR [{pair}]: {e}", file=sys.stderr)
            any_errors = True
            continue

        print(f"[{pair} {INTERVAL}] bar={result['bar_time']} signal={result['signal']} "
              f"price={result['price']}")

        pair_state = state.get(pair, {"last_alert_bar_time": None})
        already_alerted = pair_state.get("last_alert_bar_time") == result["bar_time"]

        if result["is_fresh_signal"] and result["signal"] in ("BUY", "SELL") and not already_alerted:
            emoji = "\U0001F535" if result["signal"] == "BUY" else "\U0001F534"
            decimals = 2
            label = f"[{STRATEGY_LABEL}] " if STRATEGY_LABEL else ""
            msg = (
                f"{emoji} *{label}{result['signal']} \u2014 {pair}*\n"
                f"Entry: `{result['price']:.{decimals}f}`\n"
            )
            if result["sl"] is not None:
                msg += (
                    f"SL: `{result['sl']:.{decimals}f}`\n"
                    f"TP: `{result['tp']:.{decimals}f}`\n"
                )
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
