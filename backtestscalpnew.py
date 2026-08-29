"""
backtest.py
One-off historical backtest for the SCALP strategy config. 4H bias is now
a SOFT filter (only blocks trades directly against it; doesn't need to
confirm), 15M structure (BOS/CHoCH) is the main directional gate, 5M
sweep+retest/pullback is the entry trigger. Exits are FIXED PIPS, not
ATR/R-multiples, since the goal is small scalps (e.g. 10-30 pips).

PIP CONVENTION: 1 pip = $0.10 on XAUUSD (i.e. 10-30 pips = $1.00-$3.00
move). If your broker uses 1 pip = $0.01, set PIP_SIZE=0.01 in env.

SIMPLIFICATION: this checks only ONE take-profit level per trade (volatility-
scaled, not a fixed number), not a full multi-target scale-out. It answers
"does the entry logic have edge at these distances?" before building the
partial-exit simulation.

Run as a one-off GitHub Actions workflow_dispatch job. Sends a summary to
Telegram and prints full detail (including funnel diagnostics) to the log.
"""

import os
import time
import requests
from datetime import datetime, timedelta, timezone

TWELVE_DATA_API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

SYMBOL = os.environ.get("FX_PAIRS", "XAU/USD").split(",")[0].strip()

TF_TREND = os.environ.get("TF_TREND", "4h")
TF_STRUCTURE = os.environ.get("TF_STRUCTURE", "15min")
TF_ENTRY = os.environ.get("TF_ENTRY", "5min")

SWING_LOOKBACK = int(os.environ.get("SWING_LOOKBACK", "2"))
ENTRY_MODE = os.environ.get("ENTRY_MODE", "retest_or_pullback")
RETEST_TOLERANCE_ATR_MULT = float(os.environ.get("RETEST_TOLERANCE_ATR_MULT", "0.25"))
SWING_ENTRY_MODE = os.environ.get("SWING_ENTRY_MODE", "false").lower() == "true"
ENTRY_EXPIRY_BARS = int(os.environ.get("ENTRY_EXPIRY_BARS", "5"))  # entry-TF bars, not minutes

PIP_SIZE = float(os.environ.get("PIP_SIZE", "0.10"))  # $ per pip on XAUUSD — confirm vs your broker

# TP/SL now scale with current ATR (volatility) instead of being fixed —
# bigger target in a fast market, smaller in a quiet one — but clamped to
# a pip floor/ceiling so it stays a scalp rather than drifting into swing
# territory or shrinking below what the spread would eat.
TP_ATR_MULT = float(os.environ.get("TP_ATR_MULT", "1.2"))
TP_MIN_PIPS = float(os.environ.get("TP_MIN_PIPS", "10"))
TP_MAX_PIPS = float(os.environ.get("TP_MAX_PIPS", "30"))

SL_ATR_MULT = float(os.environ.get("SL_ATR_MULT", "0.5"))
SL_MIN_PIPS = float(os.environ.get("SL_MIN_PIPS", "6"))
SL_MAX_PIPS = float(os.environ.get("SL_MAX_PIPS", "15"))

# Bias is now a SOFT filter: a setup is only blocked if bias actively
# opposes it. Set BIAS_HARD_GATE=true to restore the old strict behavior
# (bias must actively confirm structure, like the original scalp config).
BIAS_HARD_GATE = os.environ.get("BIAS_HARD_GATE", "false").lower() == "true"

ATR_PERIOD = int(os.environ.get("ATR_PERIOD", "14"))
BIAS_EMA_FAST = int(os.environ.get("BIAS_EMA_FAST", "20"))
BIAS_EMA_SLOW = int(os.environ.get("BIAS_EMA_SLOW", "50"))
BIAS_SWING_LOOKBACK = int(os.environ.get("BIAS_SWING_LOOKBACK", "20"))

# Round-trip cost in pips deducted from every trade's result (spread paid
# entering + exiting). XAUUSD spreads vary a lot by broker/session —
# check your actual broker's typical spread and set this to match.
SPREAD_PIPS = float(os.environ.get("SPREAD_PIPS", "3.0"))

MAX_HOLD_BARS = int(os.environ.get("MAX_HOLD_BARS", "40"))  # entry-TF bars before a trade times out

# Twelve Data free tier caps a single call at 5000 bars. To get a real
# sample size we page backward through history using end_date, one call
# per chunk, with a delay between calls to stay under the 8/min free
# rate limit. TREND_BARS stays a single call since 5000x4h already spans
# 2+ years — plenty for EMA/bias context.
PAGE_SIZE = 5000
HISTORY_ENTRY_BARS = int(os.environ.get("HISTORY_ENTRY_BARS", "20000"))       # ~2-3 months of 5min
HISTORY_STRUCTURE_BARS = int(os.environ.get("HISTORY_STRUCTURE_BARS", "10000"))  # ~2-3 months of 15min
HISTORY_TREND_BARS = int(os.environ.get("BACKTEST_OUTPUTSIZE", "5000"))
API_CALL_DELAY_SECONDS = float(os.environ.get("API_CALL_DELAY_SECONDS", "8"))

TWELVE_DATA_URL = "https://api.twelvedata.com/time_series"


def interval_minutes(interval):
    if interval.endswith("min"):
        return int(interval.replace("min", ""))
    if interval.endswith("h"):
        return int(interval.replace("h", "")) * 60
    if interval.endswith("day"):
        return int(interval.replace("day", "")) * 60 * 24
    raise ValueError(f"Unrecognized interval: {interval}")


# ==================== DATA ====================
def fetch_candles(symbol, interval, outputsize, end_date=None):
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize,
              "apikey": TWELVE_DATA_API_KEY, "order": "ASC"}
    if end_date:
        params["end_date"] = end_date
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


def fetch_candles_paginated(symbol, interval, target_bars):
    """Pages backward through history via end_date until target_bars is
    reached or the API stops returning new/earlier data."""
    all_candles = []
    end_date = None
    step = timedelta(minutes=interval_minutes(interval))

    while len(all_candles) < target_bars:
        batch = fetch_candles(symbol, interval, PAGE_SIZE, end_date=end_date)
        if not batch:
            break

        if all_candles:
            # drop any overlap with what we already have
            cutoff = all_candles[0]["time"]
            batch = [c for c in batch if c["time"] < cutoff]
            if not batch:
                break  # no progress — stop to avoid an infinite loop

        all_candles = batch + all_candles
        earliest = batch[0]["time"]
        end_date = (earliest - step).strftime("%Y-%m-%d %H:%M:%S")

        print(f"  [{interval}] fetched {len(batch)} bars, now have {len(all_candles)}/{target_bars} "
              f"(earliest: {earliest})")

        if len(batch) < PAGE_SIZE:
            break  # hit the start of available history

        time.sleep(API_CALL_DELAY_SECONDS)

    return all_candles


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


def clamp_pips(atr_val, mult, min_pips, max_pips):
    """ATR-scaled distance in price terms, clamped to a pip floor/ceiling."""
    if not atr_val:
        return min_pips * PIP_SIZE
    raw_pips = (atr_val * mult) / PIP_SIZE
    clamped_pips = max(min_pips, min(max_pips, raw_pips))
    return clamped_pips * PIP_SIZE


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
        if BIAS_HARD_GATE:
            want_long = bias == "bullish" and structure in ("bos_bull", "choch_bull")
            want_short = bias == "bearish" and structure in ("bos_bear", "choch_bear")
        else:
            # soft gate: structure alone qualifies a setup; bias only
            # vetoes it when directly opposed
            want_long = structure in ("bos_bull", "choch_bull") and bias != "bearish"
            want_short = structure in ("bos_bear", "choch_bear") and bias != "bullish"
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
                sl_buffer = clamp_pips(atr_val, SL_ATR_MULT, SL_MIN_PIPS, SL_MAX_PIPS)
                tp_dist = clamp_pips(atr_val, TP_ATR_MULT, TP_MIN_PIPS, TP_MAX_PIPS)
                # SL is measured from the actual entry price, not ref_level —
                # placing it off ref_level let real risk exceed the clamp
                # whenever price had already drifted from that level before
                # the retest confirmed.
                if trade_dir == "buy":
                    sl = entry_price - sl_buffer
                    tp = entry_price + tp_dist
                    risk = entry_price - sl
                else:
                    sl = entry_price + sl_buffer
                    tp = entry_price - tp_dist
                    risk = sl - entry_price

                if risk > 0:
                    target_pips = tp_dist / PIP_SIZE
                    result_pips, outcome = simulate_trade(entry_candles, ei, trade_dir, sl, tp, entry_price, target_pips)
                    net_pips = result_pips - SPREAD_PIPS
                    trades.append({"time": str(now), "direction": trade_dir, "pips": net_pips,
                                   "gross_pips": result_pips, "outcome": outcome})
                    counts["signals_fired"] += 1
                sweep = None  # consume it either way

        elif sweep and ei - sweep["bar_index"] > ENTRY_EXPIRY_BARS:
            counts["sweeps_expired"] += 1
            sweep = None

    print("Funnel diagnostics:")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    return trades


def simulate_trade(entry_candles, start_index, direction, sl, tp, entry_price, target_pips):
    for j in range(start_index + 1, min(start_index + 1 + MAX_HOLD_BARS, len(entry_candles))):
        c = entry_candles[j]
        if direction == "buy":
            hit_sl = c["low"] <= sl
            hit_tp = c["high"] >= tp
        else:
            hit_sl = c["high"] >= sl
            hit_tp = c["low"] <= tp

        if hit_sl and hit_tp:
            sl_pips = abs(entry_price - sl) / PIP_SIZE
            return -sl_pips, "sl_and_tp_same_bar_conservative_loss"
        if hit_sl:
            sl_pips = abs(entry_price - sl) / PIP_SIZE
            return -sl_pips, "sl"
        if hit_tp:
            return target_pips, "tp"

    # timed out — mark to market at last available bar
    last_close = entry_candles[min(start_index + MAX_HOLD_BARS, len(entry_candles) - 1)]["close"]
    pips = (last_close - entry_price) / PIP_SIZE if direction == "buy" else (entry_price - last_close) / PIP_SIZE
    return pips, "timeout"


# ==================== STATS ====================
def summarize(trades):
    if not trades:
        return "No signals fired over the backtest window."

    decided = [t for t in trades if t["outcome"] in ("tp", "sl", "sl_and_tp_same_bar_conservative_loss")]
    wins = [t for t in decided if t["pips"] > 0]
    losses = [t for t in decided if t["pips"] <= 0]
    timeouts = [t for t in trades if t["outcome"] == "timeout"]

    total_pips = sum(t["pips"] for t in trades)
    total_gross_pips = sum(t["gross_pips"] for t in trades)
    avg_pips = total_pips / len(trades) if trades else 0
    win_rate = (len(wins) / len(decided) * 100) if decided else 0
    gross_win = sum(t["pips"] for t in wins)
    gross_loss = abs(sum(t["pips"] for t in losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else float("inf")

    equity = 0
    peak = 0
    max_dd = 0
    for t in trades:
        equity += t["pips"]
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    def side_stats(direction):
        side = [t for t in decided if t["direction"] == direction]
        side_wins = [t for t in side if t["pips"] > 0]
        wr = (len(side_wins) / len(side) * 100) if side else 0
        return len(side), wr

    buy_n, buy_wr = side_stats("buy")
    sell_n, sell_wr = side_stats("sell")

    lines = [
        f"Backtest: {SYMBOL} SCALP config (soft bias gate: {not BIAS_HARD_GATE})",
        f"Timeframes: bias={TF_TREND} structure={TF_STRUCTURE} entry={TF_ENTRY}",
        f"Target {TP_ATR_MULT}x ATR (clamped {TP_MIN_PIPS}-{TP_MAX_PIPS} pips) / SL {SL_ATR_MULT}x ATR (clamped {SL_MIN_PIPS}-{SL_MAX_PIPS} pips), 1 pip = ${PIP_SIZE}",
        f"Spread cost: {SPREAD_PIPS} pips/trade deducted",
        f"Total signals: {len(trades)}  (decided: {len(decided)}, timed out: {len(timeouts)})",
        f"Win rate (decided, net of spread): {win_rate:.1f}%",
        f"  Buy: {buy_n} trades, {buy_wr:.1f}% win rate",
        f"  Sell: {sell_n} trades, {sell_wr:.1f}% win rate",
        f"Avg pips/trade (net): {avg_pips:.1f}",
        f"Total pips — gross: {total_gross_pips:.1f} | net of spread: {total_pips:.1f}",
        f"Profit factor (net): {profit_factor:.2f}",
        f"Max drawdown (net): {max_dd:.1f} pips",
        f"(Single-target model, volatility-scaled per trade — full TP ladder not simulated)",
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
    trend_candles = fetch_candles(SYMBOL, TF_TREND, HISTORY_TREND_BARS)
    print(f"[{TF_TREND}] fetched {len(trend_candles)} bars")

    structure_candles = fetch_candles_paginated(SYMBOL, TF_STRUCTURE, HISTORY_STRUCTURE_BARS)
    entry_candles = fetch_candles_paginated(SYMBOL, TF_ENTRY, HISTORY_ENTRY_BARS)

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
