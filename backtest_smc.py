"""
Backtests a simplified, rules-based approximation of a 4H-bias /
M15-liquidity-sweep-and-CHoCH strategy on real historical data, using
pandas. This is a ROUGH APPROXIMATION of a discretionary strategy —
several visually-judged steps (H1 key zones, exact "next liquidity pool"
target) are simplified into fixed rules. Treat this result as a weaker
signal than the earlier EMA backtest.

Rules implemented:
- 4H bias: swing-structure based (HH/HL = bullish, LH/LL = bearish),
  using only 4H bars that have fully closed (no lookahead).
- M15 swing points: 5-bar fractal, confirmed 2 bars after the extreme
  (also no lookahead — a swing point isn't "known" until confirmed).
- Liquidity sweep: price wicks beyond the most recent confirmed swing
  point opposite the bias direction, then closes back inside it.
- CHoCH confirmation: within a following window, price closes beyond
  the most recent confirmed swing point in the bias direction.
- Entry: close of the confirmation bar.
- SL: the sweep bar's extreme (wick), small buffer.
- TP: fixed 2R (approximates "next liquidity pool" — a real trader
  would target a specific visual level instead).

One trade watched at a time, per instrument, for simplicity.
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

PAIR = os.environ.get("BT_PAIR", "XAU/USD")
CHUNKS = int(os.environ.get("BT_CHUNKS", "6"))
CHUNK_SIZE = 5000
FRACTAL_WING = 2          # bars each side for swing detection
CHOCH_WINDOW = 20          # bars to wait for confirmation after a sweep
SL_BUFFER_ATR_MULT = 0.1  # small buffer beyond the sweep wick
RR_TARGET = 2.0            # fixed R:R since we can't encode "next liquidity pool"


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


def fetch_history(interval):
    all_rows = []
    end_date = None
    for i in range(CHUNKS):
        if i > 0:
            time.sleep(8)
        rows = fetch_chunk(PAIR, interval, CHUNK_SIZE, end_date)
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


def find_swings(df):
    """Returns two boolean columns: is_swing_high, is_swing_low (confirmed, no lookahead issue
    IF you only use a swing at or after index + FRACTAL_WING)."""
    n = len(df)
    is_high = [False] * n
    is_low = [False] * n
    for i in range(FRACTAL_WING, n - FRACTAL_WING):
        window_h = df["high"].iloc[i - FRACTAL_WING:i + FRACTAL_WING + 1]
        window_l = df["low"].iloc[i - FRACTAL_WING:i + FRACTAL_WING + 1]
        if df["high"].iloc[i] == window_h.max():
            is_high[i] = True
        if df["low"].iloc[i] == window_l.min():
            is_low[i] = True
    df["is_swing_high"] = is_high
    df["is_swing_low"] = is_low
    return df


def compute_4h_bias(df_15m):
    df_4h = df_15m.set_index("datetime").resample("4h").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}
    ).dropna().reset_index()
    df_4h = find_swings(df_4h)

    swing_highs, swing_lows = [], []
    bias_series = []
    current_bias = None
    for i in range(len(df_4h)):
        if i >= FRACTAL_WING and df_4h["is_swing_high"].iloc[i - FRACTAL_WING]:
            swing_highs.append(df_4h["high"].iloc[i - FRACTAL_WING])
        if i >= FRACTAL_WING and df_4h["is_swing_low"].iloc[i - FRACTAL_WING]:
            swing_lows.append(df_4h["low"].iloc[i - FRACTAL_WING])
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
                current_bias = "bullish"
            elif swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
                current_bias = "bearish"
        bias_series.append(current_bias)

    df_4h["bias"] = bias_series
    return df_4h[["datetime", "bias"]]


def simulate(df):
    df = find_swings(df)
    bias_4h = compute_4h_bias(df)

    df = pd.merge_asof(df, bias_4h.rename(columns={"datetime": "bias_time"}),
                        left_on="datetime", right_on="bias_time", direction="backward")

    confirmed_highs = []
    confirmed_lows = []
    trades = []
    watching = None

    n = len(df)
    for i in range(FRACTAL_WING, n):
        confirm_i = i - FRACTAL_WING
        if df["is_swing_high"].iloc[confirm_i]:
            confirmed_highs.append((confirm_i, df["high"].iloc[confirm_i]))
        if df["is_swing_low"].iloc[confirm_i]:
            confirmed_lows.append((confirm_i, df["low"].iloc[confirm_i]))

        bias = df["bias"].iloc[i]
        row = df.iloc[i]

        if watching:
            if watching["direction"] == "bearish":
                watching["sweep_extreme"] = max(watching["sweep_extreme"], row["high"])
            else:
                watching["sweep_extreme"] = min(watching["sweep_extreme"], row["low"])

            if i > watching["deadline_idx"]:
                watching = None
            else:
                if watching["direction"] == "bullish" and confirmed_highs:
                    target = confirmed_highs[-1][1]
                    if row["close"] > target:
                        entry = row["close"]
                        sl = watching["sweep_extreme"] - SL_BUFFER_ATR_MULT * (entry * 0.001)
                        risk = entry - sl
                        tp = entry + RR_TARGET * risk
                        trades.append({"i": i, "time": row["datetime"], "signal": "BUY",
                                        "entry": entry, "sl": sl, "tp": tp})
                        watching = None
                elif watching["direction"] == "bearish" and confirmed_lows:
                    target = confirmed_lows[-1][1]
                    if row["close"] < target:
                        entry = row["close"]
                        sl = watching["sweep_extreme"] + SL_BUFFER_ATR_MULT * (entry * 0.001)
                        risk = sl - entry
                        tp = entry - RR_TARGET * risk
                        trades.append({"i": i, "time": row["datetime"], "signal": "SELL",
                                        "entry": entry, "sl": sl, "tp": tp})
                        watching = None
                continue

        if bias == "bullish" and confirmed_lows:
            recent_low = confirmed_lows[-1][1]
            if row["low"] < recent_low and row["close"] > recent_low:
                watching = {"direction": "bullish", "sweep_extreme": row["low"],
                            "deadline_idx": i + CHOCH_WINDOW}
        elif bias == "bearish" and confirmed_highs:
            recent_high = confirmed_highs[-1][1]
            if row["high"] > recent_high and row["close"] < recent_high:
                watching = {"direction": "bearish", "sweep_extreme": row["high"],
                            "deadline_idx": i + CHOCH_WINDOW}

    resolved = []
    for t in trades:
        i = t["i"]
        for j in range(i + 1, n):
            bar = df.iloc[j]
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
    print(f"Backtesting SMC-style sweep+CHoCH strategy on {PAIR}")
    df = fetch_history("15min")
    print(f"Fetched {len(df)} bars, {df['datetime'].min()} to {df['datetime'].max()}")

    trades = simulate(df)

    if trades.empty:
        msg = f"📊 *SMC Backtest — {PAIR}*\nNo trades were generated over the tested period."
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

    summary = (
        f"📊 *SMC-style Backtest — {PAIR}*\n"
        f"4H bias + M15 liquidity sweep + CHoCH, fixed {RR_TARGET:.0f}R target\n"
        f"⚠️ Simplified approximation — H1 key zones and exact TP targeting were "
        f"dropped (too visual/judgment-based to encode). Treat as a weaker signal "
        f"than a pure indicator backtest.\n\n"
        f"Period tested: ~{span_days} days\n"
        f"Total trades: `{total}`\n"
        f"Wins: `{wins}`  Losses: `{losses}`\n"
        f"Win rate: `{win_rate*100:.1f}%`\n"
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
            send_telegram(f"⚠️ SMC backtest error: {e}")
        except Exception:
            pass
        sys.exit(1)
