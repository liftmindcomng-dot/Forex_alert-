"""
Backtests the XAUUSD Multi-Timeframe Trend-Pullback strategy:
- 4H: EMA200 direction (price vs EMA200, and EMA200 sloping)
- 15M: EMA12/26 alignment + RSI + pullback-then-confirmation
- 5M: entry on the next candle after 15M confirmation, within a window

ASSUMPTION FLAGGED: "12 points" / "24 points" for XAUUSD is ambiguous
depending on broker quote convention. This treats 1 point = $1 (so SL
= $12, TP = $24) since that matches gold's typical volatility far
better than treating it as $0.01. Adjust SL_POINTS/TP_POINTS below if
your broker's convention differs.

Other rules implemented as close to the spec as an objective backtest
allows:
- "Pullback after prior extension" — price must have moved at least
  EXTENSION_ATR_MULT x ATR away from EMA12 within the last
  EXTENSION_LOOKBACK bars, then come back within PULLBACK_ATR_MULT x
  ATR of EMA12 on the confirmation bar.
- "Confirmation candle" — the 15M bar closes in the trend direction
  (bullish close for longs, bearish for shorts).
- Session filter — soft, as specified: normal RSI threshold (50)
  inside 07:00-20:00 UTC, tightened (58/42) outside it.
- Max 5 trades/day and ~50min cooldown between entries, both enforced.
- Risk-per-trade (1% of equity) is a position-sizing detail that
  doesn't change the strategy's underlying win rate/R distribution —
  skipped in this backtest, same as all the other backtests tonight,
  since R-multiples already normalize for it.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import timedelta

import pandas as pd

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PAIR = os.environ.get("BT_PAIR", "XAU/USD")
CHUNKS = int(os.environ.get("BT_CHUNKS", "6"))
CHUNK_SIZE = 5000

EMA200_SLOPE_LOOKBACK = 5
EXTENSION_LOOKBACK = 20
EXTENSION_ATR_MULT = 1.5
PULLBACK_ATR_MULT = 0.5
ENTRY_WINDOW_MIN = 50
COOLDOWN_MIN = 50
MAX_TRADES_PER_DAY = 5

SL_POINTS = float(os.environ.get("SL_POINTS", "12"))
TP_POINTS = float(os.environ.get("TP_POINTS", "24"))
SESSION_START_UTC = int(os.environ.get("BT_SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.environ.get("BT_SESSION_END_UTC", "20"))
RSI_NORMAL = 50
RSI_STRICT_LONG = 58
RSI_STRICT_SHORT = 42


def fetch_chunk(pair, interval, outputsize, end_date=None):
    params = {"symbol": pair, "interval": interval, "outputsize": outputsize, "apikey": API_KEY}
    if end_date:
        params["end_date"] = end_date
    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")
    return data["values"]


def fetch_history(interval, chunks):
    all_rows = []
    end_date = None
    for i in range(chunks):
        if i > 0:
            time.sleep(8)
        rows = fetch_chunk(PAIR, interval, CHUNK_SIZE, end_date)
        if not rows:
            break
        all_rows.extend(rows)
        end_date = rows[-1]["datetime"]
        print(f"[{interval}] chunk {i+1}/{chunks}: {len(rows)} bars, back to {end_date}")

    df = pd.DataFrame(all_rows).drop_duplicates(subset="datetime")
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


def ema_series(s, period):
    return s.ewm(span=period, adjust=False).mean()


def rsi_series(s, period=14):
    delta = s.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    return 100 - (100 / (1 + rs))


def atr_series(df, period=14):
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()


def compute_4h_bias(df_15m):
    df_4h = df_15m.set_index("datetime").resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    df_4h["ema200"] = ema_series(df_4h["close"], 200)

    bias = []
    for i in range(len(df_4h)):
        if i < EMA200_SLOPE_LOOKBACK or pd.isna(df_4h["ema200"].iloc[i]):
            bias.append(None)
            continue
        price = df_4h["close"].iloc[i]
        e_now = df_4h["ema200"].iloc[i]
        e_prev = df_4h["ema200"].iloc[i - EMA200_SLOPE_LOOKBACK]
        if price > e_now and e_now > e_prev:
            bias.append("bullish")
        elif price < e_now and e_now < e_prev:
            bias.append("bearish")
        else:
            bias.append(None)
    df_4h["bias"] = bias
    return df_4h[["datetime", "bias"]]


def find_confirmations(df_15m):
    df = df_15m.copy()
    df["ema12"] = ema_series(df["close"], 12)
    df["ema26"] = ema_series(df["close"], 26)
    df["rsi"] = rsi_series(df["close"], 14)
    df["atr"] = atr_series(df, 14)

    bias_4h = compute_4h_bias(df)
    df = pd.merge_asof(df, bias_4h.rename(columns={"datetime": "bias_time"}),
                        left_on="datetime", right_on="bias_time", direction="backward")

    confirmations = []
    n = len(df)
    for i in range(EXTENSION_LOOKBACK + 26, n):
        row = df.iloc[i]
        bias = row["bias"]
        if bias not in ("bullish", "bearish"):
            continue
        if pd.isna(row["atr"]) or row["atr"] == 0:
            continue

        aligned = (row["ema12"] > row["ema26"]) if bias == "bullish" else (row["ema12"] < row["ema26"])
        if not aligned:
            continue

        window = df.iloc[i - EXTENSION_LOOKBACK:i]
        max_dist = (window["close"] - window["ema12"]).abs().max()
        was_extended = max_dist >= EXTENSION_ATR_MULT * row["atr"]
        if not was_extended:
            continue

        dist_now = abs(row["close"] - row["ema12"])
        pulled_back = dist_now <= PULLBACK_ATR_MULT * row["atr"]
        if not pulled_back:
            continue

        bullish_candle = row["close"] > row["open"]
        confirms = bullish_candle if bias == "bullish" else not bullish_candle
        if not confirms:
            continue

        hour = row["datetime"].hour
        in_session = SESSION_START_UTC <= hour < SESSION_END_UTC
        if bias == "bullish":
            threshold = RSI_NORMAL if in_session else RSI_STRICT_LONG
            if not (row["rsi"] > threshold):
                continue
        else:
            threshold = RSI_NORMAL if in_session else RSI_STRICT_SHORT
            if not (row["rsi"] < threshold):
                continue

        confirmations.append({"time": row["datetime"], "direction": "BUY" if bias == "bullish" else "SELL"})

    return confirmations


def simulate(df_15m, df_5m):
    confirmations = find_confirmations(df_15m)
    print(f"Found {len(confirmations)} raw 15M confirmations before entry/cooldown filtering.")

    trades = []
    last_entry_time = None
    trades_today = {}

    df_5m = df_5m.sort_values("datetime").reset_index(drop=True)

    for conf in confirmations:
        conf_time = conf["time"]
        direction = conf["direction"]

        if last_entry_time is not None and (conf_time - last_entry_time) < timedelta(minutes=COOLDOWN_MIN):
            continue

        day_key = conf_time.date()
        if trades_today.get(day_key, 0) >= MAX_TRADES_PER_DAY:
            continue

        window_end = conf_time + timedelta(minutes=ENTRY_WINDOW_MIN)
        candidates = df_5m[(df_5m["datetime"] > conf_time) & (df_5m["datetime"] <= window_end)]
        if candidates.empty:
            continue

        entry_row = candidates.iloc[0]
        entry_idx = entry_row.name
        entry = entry_row["close"]

        if direction == "BUY":
            sl = entry - SL_POINTS
            tp = entry + TP_POINTS
        else:
            sl = entry + SL_POINTS
            tp = entry - TP_POINTS

        trades.append({"entry_idx": entry_idx, "time": entry_row["datetime"], "signal": direction,
                        "entry": entry, "sl": sl, "tp": tp})
        last_entry_time = conf_time
        trades_today[day_key] = trades_today.get(day_key, 0) + 1

    print(f"{len(trades)} trades after entry-window/cooldown/max-per-day filtering.")

    resolved = []
    n5 = len(df_5m)
    for t in trades:
        i = t["entry_idx"]
        for j in range(i + 1, n5):
            bar = df_5m.iloc[j]
            if t["signal"] == "BUY":
                hit_tp = bar["high"] >= t["tp"]
                hit_sl = bar["low"] <= t["sl"]
            else:
                hit_tp = bar["low"] <= t["tp"]
                hit_sl = bar["high"] >= t["sl"]
            if hit_tp and hit_sl:
                outcome, exitp = "Loss", t["sl"]
                break
            elif hit_tp:
                outcome, exitp = "Win", t["tp"]
                break
            elif hit_sl:
                outcome, exitp = "Loss", t["sl"]
                break
        else:
            continue
        risk = abs(t["entry"] - t["sl"])
        moved = (exitp - t["entry"]) if t["signal"] == "BUY" else (t["entry"] - exitp)
        r = moved / risk if risk else 0
        resolved.append({**t, "outcome": outcome, "exit": exitp, "r": r})

    return pd.DataFrame(resolved)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main():
    print(f"Backtesting XAUUSD MTF Trend-Pullback | SL {SL_POINTS}pts TP {TP_POINTS}pts | "
          f"session {SESSION_START_UTC}-{SESSION_END_UTC} UTC")

    df_15m = fetch_history("15min", CHUNKS)
    print(f"15M: {len(df_15m)} bars, {df_15m['datetime'].min()} to {df_15m['datetime'].max()}")

    time.sleep(8)
    df_5m = fetch_history("5min", CHUNKS)
    print(f"5M: {len(df_5m)} bars, {df_5m['datetime'].min()} to {df_5m['datetime'].max()}")

    trades = simulate(df_15m, df_5m)

    if trades.empty:
        msg = f"📊 *XAUUSD MTF Trend-Pullback Backtest*\nNo trades were generated over the tested period."
        print(msg)
        send_telegram(msg)
        return

    wins = (trades["outcome"] == "Win").sum()
    losses = (trades["outcome"] == "Loss").sum()
    total = len(trades)
    win_rate = wins / total if total else 0
    avg_r = trades["r"].mean()
    total_r = trades["r"].sum()
    span_days = (df_15m["datetime"].max() - df_15m["datetime"].min()).days
    breakeven_wr = SL_POINTS / (SL_POINTS + TP_POINTS) * 100

    summary = (
        f"📊 *XAUUSD MTF Trend-Pullback Backtest*\n"
        f"4H EMA200 bias + 15M EMA12/26/RSI pullback + 5M entry, "
        f"SL {SL_POINTS}pts, TP {TP_POINTS}pts (1:2 RR)\n"
        f"⚠️ Assumes 1 point = $1 for XAUUSD — adjust SL_POINTS/TP_POINTS "
        f"if your broker's point convention differs.\n\n"
        f"Period tested: ~{span_days} days\n"
        f"Total trades: `{total}`\n"
        f"Wins: `{wins}`  Losses: `{losses}`\n"
        f"Win rate: `{win_rate*100:.1f}%` (breakeven needs `{breakeven_wr:.1f}%`)\n"
        f"Average R per trade: `{avg_r:+.2f}R`\n"
        f"Total R (sum): `{total_r:+.2f}R`"
    )
    print(summary)
    send_telegram(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        try:
            send_telegram(f"⚠️ XAUUSD MTF backtest error: {e}")
        except Exception:
            pass
        sys.exit(1)
