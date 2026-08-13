"""
Backtests a multi-confirmation strategy using pandas: combines a 4H trend
filter, a high-liquidity session-time filter, and a 15min EMA crossover +
RSI entry trigger. A trade only fires when all three agree — fewer
signals than any single-indicator version, but each one has more context
behind it. Reports results via Telegram. One-off tool, manual trigger only.

Rules:
- 4H trend filter: 4H close above 4H EMA(TREND_EMA_PERIOD) = bullish bias,
  below = bearish bias. Only the most recently CLOSED 4H bar is used
  (no lookahead).
- Session filter: only bars whose UTC hour falls in
  [SESSION_START_UTC, SESSION_END_UTC) are eligible.
- Entry trigger: EMA_FAST/EMA_SLOW crossover on the 15min chart, filtered
  by RSI, same as the original strategy — but only taken in the direction
  of the 4H bias, and only inside the session window.
- SL/TP: ATR multiples on the 15min chart.
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
CHUNKS = int(os.environ.get("BT_CHUNKS", "6"))
CHUNK_SIZE = 5000

TREND_EMA_PERIOD = int(os.environ.get("BT_TREND_EMA_PERIOD", "50"))
SESSION_START_UTC = int(os.environ.get("BT_SESSION_START_UTC", "12"))
SESSION_END_UTC = int(os.environ.get("BT_SESSION_END_UTC", "16"))
EMA_FAST = int(os.environ.get("BT_EMA_FAST", "12"))
EMA_SLOW = int(os.environ.get("BT_EMA_SLOW", "26"))
RSI_OVERBOUGHT = float(os.environ.get("BT_RSI_OVERBOUGHT", "70"))
RSI_OVERSOLD = float(os.environ.get("BT_RSI_OVERSOLD", "30"))
ATR_SL_MULT = float(os.environ.get("BT_ATR_SL_MULT", "1.5"))
ATR_TP_MULT = float(os.environ.get("BT_ATR_TP_MULT", "3"))


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
        rows = fetch_chunk(PAIR, "15min", CHUNK_SIZE, end_date)
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


def compute_4h_trend(df_15m):
    df_4h = df_15m.set_index("datetime").resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    df_4h["ema_trend"] = df_4h["close"].ewm(span=TREND_EMA_PERIOD, adjust=False).mean()
    df_4h["bias"] = df_4h.apply(
        lambda r: "bullish" if r["close"] > r["ema_trend"] else "bearish", axis=1
    )
    return df_4h[["datetime", "bias"]]


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

    df["in_session"] = df["datetime"].dt.hour.between(SESSION_START_UTC, SESSION_END_UTC - 1)
    return df


def simulate_trades(df):
    trades = []
    n = len(df)
    for i in range(60, n - 1):
        row = df.iloc[i]
        if pd.isna(row["atr"]) or pd.isna(row["rsi"]) or pd.isna(row["bias"]):
            continue
        if not row["in_session"]:
            continue

        signal = None
        if row["bias"] == "bullish" and row["crossed_up"] and row["rsi"] < RSI_OVERBOUGHT:
            signal = "BUY"
        elif row["bias"] == "bearish" and row["crossed_down"] and row["rsi"] > RSI_OVERSOLD:
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
    url
