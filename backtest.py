"""
Backtests the EMA crossover + RSI filter strategy against real historical
data using pandas, and reports win rate / average R via Telegram. This is
a one-off tool (triggered manually from the Actions tab) — it does not run
on a schedule and does not place any trades.

Fetches several months of history in chunks (paginating with Twelve Data's
end_date parameter) since one request is capped at ~5000 bars.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse

import pandas as pd

# ---- config ----
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PAIR = os.environ.get("BT_PAIR", "XAU/USD")
INTERVAL = os.environ.get("BT_INTERVAL", "5min")
EMA_FAST = int(os.environ.get("BT_EMA_FAST", "2"))
EMA_SLOW = int(os.environ.get("BT_EMA_SLOW", "5"))
RSI_OVERBOUGHT = float(os.environ.get("BT_RSI_OVERBOUGHT", "80"))
RSI_OVERSOLD = float(os.environ.get("BT_RSI_OVERSOLD", "20"))
ATR_SL_MULT = float(os.environ.get("BT_ATR_SL_MULT", "1.0"))
ATR_TP_MULT = float(os.environ.get("BT_ATR_TP_MULT", "1.5"))
CHUNKS = int(os.environ.get("BT_CHUNKS", "6"))       # ~6 x 5000 bars of 5min ≈ 3-4 months
CHUNK_SIZE = 5000


def fetch_chunk(pair, interval, outputsize, end_date=None):
    params = {
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": API_KEY,
    }
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
            time.sleep(8)  # stay under free-tier rate limit
        rows = fetch_chunk(PAIR, INTERVAL, CHUNK_SIZE, end_date)
        if not rows:
            break
        all_rows.extend(rows)
        oldest = rows[-1]["datetime"]  # API returns newest-first
        end_date = oldest
        print(f"Chunk {i+1}/{CHUNKS}: {len(rows)} bars, back to {oldest}")

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset="datetime")
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def compute_indicators(df):
    df["ema_fast"] = df["close"].ewm(span=EMA_FAST, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=EMA_SLOW, adjust=False).mean()

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

    df["crossed_up"] = (df["ema_fast"].shift(1) <= df["ema_slow"].shift(1)) & (df["ema_fast"] > df["ema_slow"])
    df["crossed_down"] = (df["ema_fast"].shift(1) >= df["ema_slow"].shift(1)) & (df["ema_fast"] < df["ema_slow"])
    return df


def simulate_trades(df):
    trades = []
    n = len(df)
    for i in range(20, n - 1):
        row = df.iloc[i]
        if pd.isna(row["atr"]) or pd.isna(row["rsi"]):
            continue

        signal = None
        if row["crossed_up"] and row["rsi"] < RSI_OVERBOUGHT:
            signal = "BUY"
        elif row["crossed_down"] and row["rsi"] > RSI_OVERSOLD:
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

        outcome = None
        exit_price = None
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
        r_multiple = moved / risk if risk else 0

        trades.append({
            "time": row["datetime"], "signal": signal, "entry": entry,
            "sl": sl, "tp": tp, "exit": exit_price, "outcome": outcome, "r": r_multiple,
        })

    return pd.DataFrame(trades)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main():
    print(f"Backtesting {PAIR} | EMA{EMA_FAST}/{EMA_SLOW} | RSI {RSI_OVERBOUGHT:.0f}/{RSI_OVERSOLD:.0f} "
          f"| SL {ATR_SL_MULT}x ATR | TP {ATR_TP_MULT}x ATR | interval {INTERVAL}")

    df = fetch_history()
    print(f"Fetched {len(df)} bars, {df['datetime'].min()} to {df['datetime'].max()}")

    df = compute_indicators(df)
    trades = simulate_trades(df)

    if trades.empty:
        msg = f"📊 *Backtest — {PAIR}*\nNo trades were generated by this strategy over the tested period."
        print(msg)
        send_telegram(msg)
        return

    wins = (trades["outcome"] == "Win").sum()
    losses = (trades["outcome"] == "Loss").sum()
    total = len(trades)
    win_rate = wins / total if total else 0
    avg_r = trades["r"].mean()
    total_r = trades["r"].sum()
    best = trades["r"].max()
    worst = trades["r"].min()

    span_days = (df["datetime"].max() - df["datetime"].min()).days

    summary = (
        f"📊 *Backtest results — {PAIR}*\n"
        f"Strategy: EMA{EMA_FAST}/{EMA_SLOW}, RSI {RSI_OVERBOUGHT:.0f}/{RSI_OVERSOLD:.0f}, "
        f"SL {ATR_SL_MULT}x ATR, TP {ATR_TP_MULT}x ATR\n"
        f"Period tested: ~{span_days} days ({INTERVAL} candles)\n\n"
        f"Total trades: `{total}`\n"
        f"Wins: `{wins}`  Losses: `{losses}`\n"
        f"Win rate: `{win_rate*100:.1f}%`\n"
        f"Average R per trade: `{avg_r:+.2f}R`\n"
        f"Total R (sum): `{total_r:+.2f}R`\n"
        f"Best trade: `{best:+.2f}R`  Worst: `{worst:+.2f}R`\n\n"
        f"Note: assumes worst-case fill when a candle touches both SL and TP, "
        f"and ignores spread/slippage — real results will be somewhat worse than this."
    )
    print(summary)
    send_telegram(summary)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        err = f"Backtest failed: {e}"
        print(err, file=sys.stderr)
        try:
            send_telegram(f"⚠️ Backtest error: {e}")
        except Exception:
            pass
        sys.exit(1)
