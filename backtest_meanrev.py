"""
Backtests a mean-reversion strategy using pandas: enter when RSI has been
in oversold/overbought territory and then crosses back through the
threshold (confirming a turn, not just touching the extreme). Reports
results via Telegram. One-off tool, manual trigger only.

Rules:
- BUY: RSI was below RSI_OVERSOLD on the previous bar, and closes back
  above it on this bar (a confirmed reversal up, not a falling knife).
- SELL: mirror image using RSI_OVERBOUGHT.
- SL/TP: fixed ATR multiples (defaults favor a higher win rate, smaller
  targets — typical of mean reversion).
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse

import pandas as pd

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PAIR = os.environ.get("BT_PAIR", "EUR/USD")
INTERVAL = os.environ.get("BT_INTERVAL", "15min")
RSI_OVERSOLD = float(os.environ.get("BT_RSI_OVERSOLD", "30"))
RSI_OVERBOUGHT = float(os.environ.get("BT_RSI_OVERBOUGHT", "70"))
ATR_SL_MULT = float(os.environ.get("BT_ATR_SL_MULT", "1.0"))
ATR_TP_MULT = float(os.environ.get("BT_ATR_TP_MULT", "1.0"))
CHUNKS = int(os.environ.get("BT_CHUNKS", "6"))
CHUNK_SIZE = 5000


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


def fetch_history():
    all_rows = []
    end_date = None
    for i in range(CHUNKS):
        if i > 0:
            time.sleep(8)
        rows = fetch_chunk(PAIR, INTERVAL, CHUNK_SIZE, end_date)
        if not rows:
            break
        all_rows.extend(rows)
        end_date = rows[-1]["datetime"]
        print(f"Chunk {i+1}/{CHUNKS}: {len(rows)} bars, back to {end_date}")

    df = pd.DataFrame(all_rows).drop_duplicates(subset="datetime")
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


def compute_indicators(df):
    delta = df["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, 1e-10)
    df["rsi"] = 100 - (100 / (1 + rs))

    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()

    df["buy_signal"] = (df["rsi"].shift(1) < RSI_OVERSOLD) & (df["rsi"] >= RSI_OVERSOLD)
    df["sell_signal"] = (df["rsi"].shift(1) > RSI_OVERBOUGHT) & (df["rsi"] <= RSI_OVERBOUGHT)
    return df


def simulate_trades(df):
    trades = []
    n = len(df)
    for i in range(20, n - 1):
        row = df.iloc[i]
        if pd.isna(row["atr"]):
            continue

        signal = None
        if row["buy_signal"]:
            signal = "BUY"
        elif row["sell_signal"]:
            signal = "SELL"
        if signal is None:
            continue

        entry = row["close"]
        atr_val = row["atr"]
        if signal == "BUY":
            sl = entry - ATR_SL_MULT * atr_val
            tp = entry + ATR_TP_MULT * atr_val
        else:
            sl = entry + ATR_SL_MULT * atr_val
            tp = entry - ATR_TP_MULT * atr_val

        outcome = exit_price = None
        for j in range(i + 1, n):
            bar = df.iloc[j]
            hit_tp = bar["high"] >= tp if signal == "BUY" else bar["low"] <= tp
            hit_sl = bar["low"] <= sl if signal == "BUY" else bar["high"] >= sl
            if hit_tp and hit_sl:
                outcome, exit_price = "Loss", sl
                break
            elif hit_tp:
                outcome, exit_price = "Win", tp
                break
            elif hit_sl:
                outcome, exit_price = "Loss", sl
                break
        if outcome is None:
            continue

        risk = abs(entry - sl)
        moved = (exit_price - entry) if signal == "BUY" else (entry - exit_price)
        r = moved / risk if risk else 0

        trades.append({
            "time": row["datetime"], "signal": signal, "entry": entry,
            "sl": sl, "tp": tp, "exit": exit_price, "outcome": outcome, "r": r,
        })

    return pd.DataFrame(trades)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main():
    print(f"Backtesting mean-reversion on {PAIR} | RSI {RSI_OVERSOLD:.0f}/{RSI_OVERBOUGHT:.0f} "
          f"| SL {ATR_SL_MULT}x ATR | TP {ATR_TP_MULT}x ATR | interval {INTERVAL}")

    df = fetch_history()
    print(f"Fetched {len(df)} bars, {df['datetime'].min()} to {df['datetime'].max()}")

    df = compute_indicators(df)
    trades = simulate_trades(df)

    if trades.empty:
        msg = f"📊 *Mean-Reversion Backtest — {PAIR}*\nNo trades were generated over the tested period."
        print(msg)
        send_telegram(msg)
        return

    wins = (trades["outcome"] == "Win").sum()
    losses = (trades["outcome"] == "Loss").sum()
    total = len(trades)
    win_rate = wins / total if total else 0
    avg_r = trades["r"].mean()
    total_r = trades["r"].sum()
    span_days = (df["datetime"].max() - df["datetime"].min()).days
    breakeven_wr = ATR_SL_MULT / (ATR_SL_MULT + ATR_TP_MULT) * 100

    summary = (
        f"📊 *Mean-Reversion Backtest — {PAIR}*\n"
        f"RSI {RSI_OVERSOLD:.0f}/{RSI_OVERBOUGHT:.0f} reversal, SL {ATR_SL_MULT}x ATR, TP {ATR_TP_MULT}x ATR\n"
        f"Period tested: ~{span_days} days ({INTERVAL} candles)\n\n"
        f"Total trades: `{total}`\n"
        f"Wins: `{wins}`  Losses: `{losses}`\n"
        f"Win rate: `{win_rate*100:.1f}%` (breakeven needs `{breakeven_wr:.1f}%`)\n"
        f"Average R per trade: `{avg_r:+.2f}R`\n"
        f"Total R (sum): `{total_r:+.2f}R`\n\n"
        f"Note: worst-case fill assumed on same-bar SL+TP touches; ignores spread/slippage."
    )
    print(summary)
    send_telegram(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        try:
            send_telegram(f"⚠️ Mean-reversion backtest error: {e}")
        except Exception:
            pass
        sys.exit(1)
