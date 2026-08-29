"""
backtest.py
One-off historical backtest for the SCALP strategy config (4H bias -> 15M
structure -> 5M sweep+retest/pullback entry). Pulls the max free-tier
history in a single call per timeframe (outputsize=5000) and walks forward
bar-by-bar on the entry timeframe, simulating each signal against a single
target (TP_MULTIPLES[0]) vs. the stop.

SIMPLIFICATION: this checks only the FIRST take-profit level, not your
full multi-TP scale-out. It answers "does the entry logic have edge?"
before you build the more complex partial-exit simulation.

Run as a one-off GitHub Actions workflow_dispatch job. Sends a summary to
Telegram and prints full detail to the Actions log.
"""

import os
import requests
from datetime import datetime, timezone

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOL = os.environ.get("FX_PAIRS", "XAU/USD").split(",")[0].strip()

TF_TREND = os.environ.get("TF_TREND", "4h")
TF_STRUCTURE = os.environ.get("TF_STRUCTURE", "15min")
TF_ENTRY = os.environ.get("TF_ENTRY", "5min")

SWING_LOOKBACK = int(os.environ.get("SWING_LOOKBACK", "2"))
SL_BUFFER_ATR_MULT = float(os.environ.get("SL_BUFFER_ATR_MULT", "0.08"))
ENTRY_MODE = os.environ.get("ENTRY_MODE", "retest_or_pullback")
RETEST_TOLERANCE_ATR_MULT = float(os.environ.get("RETEST_TOLERANCE_ATR_MULT", "0.15"))
SWING_ENTRY_MODE = os.environ.get("SWING_ENTRY_MODE", "false").lower() == "true"
ENTRY_EXPIRY_BARS = int(os.environ.get("ENTRY_EXPIRY_BARS", "3"))  # entry-TF bars, not minutes

TP_MULTIPLES = [float(x) for x in os.environ.get("TP_MULTIPLES", "0.5,1,1.5,2").split(",")]
TARGET_R = TP_MULTIPLES[0]  # single-target simplification

ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
BIAS_EMA_FAST = int(os.environ.get("BIAS_EMA_FAST", "20"))
BIAS_EMA_SLOW = int(os.environ.get("BIAS_EMA_SLOW", "50"))
BIAS_SWING_LOOKBACK = int(os.environ.get("BIAS_SWING_LOOKBACK", "20"))

MAX_HOLD_BARS = int(os.environ.get("MAX_HOLD_BARS", "40"))  # entry-TF bars before a trade times out
OUTPUTSIZE = int(os.environ.get("BACKTEST_OUTPUTSIZE", "5000"))  # free-tier max per request

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"


# ==================== DATA ====================
def fetch_candles(symbol, interval, outputsize):
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVE_DATA_API_KEY, "order": "ASC"}
    resp = requests.get(TWELVE_DATA_URL, params=params, timeout=30)
    data = resp.json()
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error for {symbol} {interval}: {data}")
    candles = []
    for v in data["values"]:
        candles.append({
            "time": datetime.strptime(v["datetime"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
            "open": float(v["open"]), "high": float(v["high"]),
            "low": float(v["low"]), "close": float(v["close"]),
        })
    return candles


# ==================== INDICATORS (same logic as forex_alert.py) ====================
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def atr(candles, period=ATR_PERIOD):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_c = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
    return sum(trs[-period:]) / period


def find_last_swing(candles, swing_bars):
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    swing_high = swing_low = None
    for i in range(len(candles) - swing_bars - 1, swing_bars - 1, -1):
        wh = highs[i - swing_bars:i + swing_bars + 1]
        wl = lows[i - swing_bars:i + swing_bars + 1]
        if swing_high is None and highs[i] == max(wh):
            swing_high = highs[i]
        if swing_low is None and lows[i] == min(wl):
            swing_low = lows[i]
        if swing_high is not None and swing_low is not None:
            break
    return swing_high, swing_low


def compute_bias(candles):
    closes = [c["close"] for c in candles]
    if len(closes) < BIAS_EMA_SLOW + 1:
        return "neutral"
    ema_fast, ema_slow = ema(closes, BIAS_EMA_FAST), ema(closes, BIAS_EMA_SLOW)
    if ema_fast is None or ema_slow is None:
        return "neutral"
    window = candles[-BIAS_SWING_LOOKBACK:]
    half = len(window) // 2
    older, recent = window[:half], window[half:]
    higher_low = min(c["low"] for c in recent) > min(c["low"] for c in older)
    lower_high = max(c["high"] for c in recent) < max(c["high"] for c in older)
    if ema_fast > ema_slow and higher_low:
        return "bullish"
    if ema_fast < ema_slow and lower_high:
        return "bearish"
    return "neutral"


def compute_structure(candles, bias):
    swing_high, swing_low = find_last_swing(candles, SWING_LOOKBACK)
    if swing_high is None or swing_low is None:
        return "none"
    last_close = candles[-1]["close"]
    if last_close > swing_high:
        return "bos_bull" if bias == "bullish" else "choch_bull"
    if last_close < swing_low:
        return "bos_bear" if bias == "bearish" else "choch_bear"
    return "none"


# ==================== WALK-FORWARD SIMULATION ====================
def run_backtest(trend_candles, structure_candles, entry_candles):
    trades = []
    sweep = None  # {"direction","level","bar_index"}

    ti = si = 0  # pointers into trend/structure candle lists
    bias = "neutral"
    structure = "none"

    counts = {"bars": 0, "bias_bullish": 0, "bias_bearish": 0, "bias_neutral": 0,
              "structure_aligned": 0, "sweeps_detected": 0, "sweeps_expired": 0,
              "retest_checks": 0, "signals_fired": 0}

    for ei in range(BIAS_SWING_LOOKBACK, len(entry_candles)):
        now = entry_candles[ei]["time"]

        # advance trend pointer to the latest trend candle closed before `now`
        while ti + 1 < len(trend_candles) and trend_candles[ti + 1]["time"] <= now:
            ti += 1
        if ti >= BIAS_EMA_SLOW:
            bias = compute_bias(trend_candles[:ti + 1])

        # advance structure pointer similarly
        while si + 1 < len(structure_candles) and structure_candles[si + 1]["time"] <= now:
            si += 1
        if si >= SWING_LOOKBACK * 2 + 5:
            structure = compute_structure(structure_candles[:si + 1], bias)

        counts["bars"] += 1
        counts[f"bias_{bias}"] += 1

        window = entry_candles[max(0, ei - 60):ei + 1]
        atr_val = atr(window)
        swing_high, swing_low = find_last_swing(window, SWING_LOOKBACK)

        last = entry_candles[ei]
        want_long = bias == "bullish" and structure in ("bos_bull", "choch_bull")
        want_short = bias == "bearish" and structure in ("bos_bear", "choch_bear")
        if want_long or want_short:
            counts["structure_aligned"] += 1

        # detect sweep
        if swing_low is not None and want_long and last["low"] < swing_low < last["close"]:
            sweep = {"direction": "long", "level": swing_low, "bar_index": ei}
            counts["sweeps_detected"] += 1
        if swing_high is not None and want_short and last["high"] > swing_high > last["close"]:
            sweep = {"direction": "short", "level": swing_high, "bar_index": ei}
            counts["sweeps_detected"] += 1

        # check entry off an active sweep
        if sweep and ei - sweep["bar_index"] <= ENTRY_EXPIRY_BARS and ei > sweep["bar_index"]:
            counts["retest_checks"] += 1
            tolerance = RETEST_TOLERANCE_ATR_MULT * (atr_val or 0)
            direction, level = sweep["direction"], sweep["level"]
            near_level = abs(last["close"] - level) <= tolerance
            bull_candle = last["close"] > last["open"]
            bear_candle = last["close"] < last["open"]

            fired = None
            if "retest" in ENTRY_MODE and near_level:
                if direction == "long" and bull_candle:
                    fired = ("buy", level)
                elif direction == "short" and bear_candle:
                    fired = ("sell", level)
            if not fired and "pullback" in ENTRY_MODE and not SWING_ENTRY_MODE:
                closes = [c["close"] for c in window]
                fast_ema = ema(closes, BIAS_EMA_FAST)
                if fast_ema and abs(last["close"] - fast_ema) <= tolerance:
                    if direction == "long" and bull_candle:
                        fired = ("buy", fast_ema)
                    elif direction == "short" and bear_candle:
                        fired = ("sell", fast_ema)

            if fired:
                trade_dir, ref_level = fired
                entry_price = last["close"]
                sl_buffer = SL_BUFFER_ATR_MULT * (atr_val or 0)
                if trade_dir == "buy":
                    sl = ref_level - sl_buffer
                    risk = entry_price - sl
                    tp = entry_price + risk * TARGET_R
                else:
                    sl = ref_level + sl_buffer
                    risk = sl - entry_price
                    tp = entry_price - risk * TARGET_R

                if risk > 0:
                    result_r, outcome = simulate_trade(entry_candles, ei, trade_dir, sl, tp, entry_price, risk)
                    trades.append({"time": str(now), "direction": trade_dir, "r": result_r, "outcome": outcome})
                    counts["signals_fired"] += 1
                sweep = None  # consume it either way

        elif sweep and ei - sweep["bar_index"] > ENTRY_EXPIRY_BARS:
            counts["sweeps_expired"] += 1
            sweep = None

    print("Funnel diagnostics:")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    return trades


def simulate_trade(entry_candles, start_index, direction, sl, tp, entry_price, risk):
    for j in range(start_index + 1, min(start_index + 1 + MAX_HOLD_BARS, len(entry_candles))):
        c = entry_candles[j]
        if direction == "buy":
            hit_sl = c["low"] <= sl
            hit_tp = c["high"] >= tp
        else:
            hit_sl = c["high"] >= sl
            hit_tp = c["low"] <= tp

        if hit_sl and hit_tp:
            return -1.0, "sl_and_tp_same_bar_conservative_loss"
        if hit_sl:
            return -1.0, "sl"
        if hit_tp:
            return TARGET_R, "tp"

    # timed out — mark to market at last available bar
    last_close = entry_candles[min(start_index + MAX_HOLD_BARS, len(entry_candles) - 1)]["close"]
    r = (last_close - entry_price) / risk if direction == "buy" else (entry_price - last_close) / risk
    return r, "timeout"


# ==================== STATS ====================
def summarize(trades):
    if not trades:
        return "No signals fired over the backtest window."

    decided = [t for t in trades if t["outcome"] in ("tp", "sl")]
    wins = [t for t in decided if t["r"] > 0]
    losses = [t for t in decided if t["r"] <= 0]
    timeouts = [t for t in trades if t["outcome"] == "timeout"]

    total_r = sum(t["r"] for t in trades)
    avg_r = total_r / len(trades) if trades else 0
    win_rate = (len(wins) / len(decided) * 100) if decided else 0
    gross_win = sum(t["r"] for t in wins)
    gross_loss = abs(sum(t["r"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t["r"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    lines = [
        f"Backtest: {SYMBOL} SCALP config",
        f"Timeframes: bias={TF_TREND} structure={TF_STRUCTURE} entry={TF_ENTRY}",
        f"Total signals: {len(trades)}  (decided: {len(decided)}, timed out: {len(timeouts)})",
        f"Win rate (decided only): {win_rate:.1f}%",
        f"Avg R per trade (all signals): {avg_r:.2f}",
        f"Total R: {total_r:.2f}",
        f"Profit factor: {profit_factor:.2f}",
        f"Max drawdown: {max_dd:.2f}R",
        f"(Single-target model @ {TARGET_R}R — real multi-TP scale-out not simulated)",
    ]
    return "\n".join(lines)


def send_telegram_message(text):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=20)
    if not resp.ok:
        print(f"Telegram send failed: {resp.text}")


def main():
    print(f"Fetching history for {SYMBOL}...")
    trend_candles = fetch_candles(SYMBOL, TF_TREND, OUTPUTSIZE)
    structure_candles = fetch_candles(SYMBOL, TF_STRUCTURE, OUTPUTSIZE)
    entry_candles = fetch_candles(SYMBOL, TF_ENTRY, OUTPUTSIZE)

    print(f"Bars fetched — trend:{len(trend_candles)} structure:{len(structure_candles)} entry:{len(entry_candles)}")
    print(f"Entry TF date range: {entry_candles[0]['time']} to {entry_candles[-1]['time']}")

    trades = run_backtest(trend_candles, structure_candles, entry_candles)

    for t in trades:
        print(t)

    summary = summarize(trades)
    print("\n" + summary)
    send_telegram_message(summary)


if __name__ == "__main__":
    main()
