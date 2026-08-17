"""
Backtests a 3-timeframe Breakout + Retest strategy on real historical
data from Twelve Data:

  4H    -> directional bias only. Swing structure (fractal highs/lows):
           higher-high + higher-low = bullish bias, lower-high +
           lower-low = bearish bias. No signals come from this
           timeframe, it's purely a filter on which direction trades are
           allowed to fire.

  15min -> where the actual setup forms, in the direction the 4H bias
           allows only. A "zone of interest" is a price area touched by
           3 or more candle highs/lows within a small tolerance. Once a
           valid zone exists, a candle closing beyond it is the
           breakout; a later candle wicking back into the zone (without
           closing back through it) is the retest.

  5min  -> once the 15min retest holds, this is where entry timing gets
           refined: the first 5-minute candle that closes back beyond
           the zone, after the retest, triggers the entry. This is also
           why trades naturally take more than a candle or two to
           resolve \u2014 timing is refined well below the timeframe the
           structure was built on.

This is a simplified, rules-based approximation of a discretionary
multi-timeframe strategy \u2014 "3+ touches" is measured as any candle
high/low (not just fractal swing points) landing within a tolerance
band of a level, which is closer to how a trader would eyeball a zone
by eye than a strict fractal-only count. Treat results as indicative,
not a substitute for chart review.

SETUP:
    export TWELVE_DATA_API_KEY=your_real_key
    export TELEGRAM_BOT_TOKEN=your_real_token
    export TELEGRAM_CHAT_ID=your_real_chat_id

USAGE:
    python backtest_mtf.py --pair XAU/USD --chunks 4
"""

import argparse
import json
import time
import urllib.request
import urllib.parse

import pandas as pd

API_KEY = None
BOT_TOKEN = None
CHAT_ID = None

import os
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

CHUNK_SIZE = 5000
ZONE_TOLERANCE_PCT = float(os.environ.get("ZONE_TOLERANCE_PCT", "0.0015"))  # 0.15% of price
MIN_TOUCHES = int(os.environ.get("MIN_TOUCHES", "3"))
RETEST_WINDOW_15M = int(os.environ.get("RETEST_WINDOW_15M", "20"))   # ~5 hours
ENTRY_WINDOW_5M = int(os.environ.get("ENTRY_WINDOW_5M", "36"))       # ~3 hours
RR_RATIO = float(os.environ.get("RR_RATIO", "2.0"))
SL_BUFFER_PCT = float(os.environ.get("SL_BUFFER_PCT", "0.0008"))
SWING_LEFT = 2
SWING_RIGHT = 2


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


def fetch_df(pair, interval, chunks):
    all_rows = []
    end_date = None
    for i in range(chunks):
        if i > 0:
            time.sleep(8)
        rows = fetch_chunk(pair, interval, CHUNK_SIZE, end_date)
        if not rows:
            break
        all_rows.extend(rows)
        end_date = rows[-1]["datetime"]
        print(f"  [{interval}] chunk {i + 1}/{chunks}: {len(rows)} bars, back to {end_date}", flush=True)

    df = pd.DataFrame(all_rows).drop_duplicates(subset="datetime")
    df["datetime"] = pd.to_datetime(df["datetime"])
    for col in ["open", "high", "low", "close"]:
        df[col] = df[col].astype(float)
    return df.sort_values("datetime").reset_index(drop=True)


def find_swings(df, left=SWING_LEFT, right=SWING_RIGHT):
    n = len(df)
    is_high = [False] * n
    is_low = [False] * n
    highs, lows = df["high"].values, df["low"].values
    for i in range(left, n - right):
        wh = highs[i - left:i + right + 1]
        wl = lows[i - left:i + right + 1]
        if highs[i] == wh.max():
            is_high[i] = True
        if lows[i] == wl.min():
            is_low[i] = True
    df = df.copy()
    df["is_swing_high"] = is_high
    df["is_swing_low"] = is_low
    return df


def compute_4h_bias(df4h):
    df4h = find_swings(df4h)
    swing_highs, swing_lows = [], []
    bias_series = []
    current_bias = None
    n = len(df4h)
    for i in range(n):
        confirm_i = i - SWING_RIGHT
        if confirm_i >= 0:
            if df4h["is_swing_high"].iloc[confirm_i]:
                swing_highs.append(df4h["high"].iloc[confirm_i])
            if df4h["is_swing_low"].iloc[confirm_i]:
                swing_lows.append(df4h["low"].iloc[confirm_i])
        if len(swing_highs) >= 2 and len(swing_lows) >= 2:
            if swing_highs[-1] > swing_highs[-2] and swing_lows[-1] > swing_lows[-2]:
                current_bias = "bullish"
            elif swing_highs[-1] < swing_highs[-2] and swing_lows[-1] < swing_lows[-2]:
                current_bias = "bearish"
        bias_series.append(current_bias)
    return pd.DataFrame({"datetime": df4h["datetime"], "bias": bias_series})


def find_zones(highs, lows, up_to_i, tolerance_pct, min_touches, lookback=200):
    """
    Cluster recent candle highs/lows into zones. Returns (nearest_resistance,
    nearest_support) as (price, touch_count) or None if no valid zone.
    Simple greedy clustering: sort points, group within tolerance of the
    group's running average.
    """
    start = max(0, up_to_i - lookback)
    points = list(highs[start:up_to_i]) + list(lows[start:up_to_i])
    if not points:
        return None, None
    points_sorted = sorted(points)
    zones = []
    group = [points_sorted[0]]
    for p in points_sorted[1:]:
        avg = sum(group) / len(group)
        if abs(p - avg) <= avg * tolerance_pct:
            group.append(p)
        else:
            zones.append(group)
            group = [p]
    zones.append(group)

    valid = [(sum(g) / len(g), len(g)) for g in zones if len(g) >= min_touches]
    if not valid:
        return None, None

    ref_price = (highs[up_to_i - 1] + lows[up_to_i - 1]) / 2
    above = [z for z in valid if z[0] > ref_price]
    below = [z for z in valid if z[0] < ref_price]
    nearest_res = min(above, key=lambda z: z[0] - ref_price) if above else None
    nearest_sup = min(below, key=lambda z: ref_price - z[0]) if below else None
    return nearest_res, nearest_sup


def simulate(pair, df4h, df15, df5):
    bias_df = compute_4h_bias(df4h)
    df15 = pd.merge_asof(
        df15, bias_df.rename(columns={"datetime": "bias_time"}),
        left_on="datetime", right_on="bias_time", direction="backward"
    )

    highs15, lows15, closes15, times15 = (
        df15["high"].values, df15["low"].values, df15["close"].values, df15["datetime"].values
    )
    n15 = len(df15)

    times5 = df5["datetime"].values
    highs5, lows5, closes5 = df5["high"].values, df5["low"].values, df5["close"].values
    n5 = len(df5)

    trades = []
    i = MIN_TOUCHES + 5
    while i < n15:
        bias = df15["bias"].iloc[i]
        if bias not in ("bullish", "bearish"):
            i += 1
            continue

        res_zone, sup_zone = find_zones(highs15, lows15, i, ZONE_TOLERANCE_PCT, MIN_TOUCHES)

        breakout_i = None
        zone_level = None
        direction = None
        if bias == "bullish" and res_zone and closes15[i] > res_zone[0]:
            breakout_i, zone_level, direction = i, res_zone[0], "BUY"
        elif bias == "bearish" and sup_zone and closes15[i] < sup_zone[0]:
            breakout_i, zone_level, direction = i, sup_zone[0], "SELL"

        if breakout_i is None:
            i += 1
            continue

        tol = zone_level * ZONE_TOLERANCE_PCT
        retest_i = None
        for j in range(breakout_i + 1, min(breakout_i + 1 + RETEST_WINDOW_15M, n15)):
            if direction == "BUY" and lows15[j] <= zone_level + tol:
                retest_i = j
                break
            if direction == "SELL" and highs15[j] >= zone_level - tol:
                retest_i = j
                break

        if retest_i is None:
            i = breakout_i + 1
            continue

        retest_time = times15[retest_i]
        retest_extreme = lows15[retest_i] if direction == "BUY" else highs15[retest_i]

        start5 = None
        for k in range(n5):
            if times5[k] >= retest_time:
                start5 = k
                break
        if start5 is None:
            i = retest_i + 1
            continue

        entry_k = None
        for k in range(start5, min(start5 + ENTRY_WINDOW_5M, n5)):
            if direction == "BUY" and closes5[k] > zone_level:
                entry_k = k
                break
            if direction == "SELL" and closes5[k] < zone_level:
                entry_k = k
                break

        if entry_k is None:
            i = retest_i + 1
            continue

        entry_price = closes5[entry_k]
        buffer = entry_price * SL_BUFFER_PCT
        if direction == "BUY":
            sl = retest_extreme - buffer
            risk = entry_price - sl
            tp = entry_price + RR_RATIO * risk if risk > 0 else None
        else:
            sl = retest_extreme + buffer
            risk = sl - entry_price
            tp = entry_price - RR_RATIO * risk if risk > 0 else None

        if not tp or risk <= 0:
            i = retest_i + 1
            continue

        outcome = "OPEN"
        resolve_minutes = None
        resolve_time = None
        for m in range(entry_k + 1, min(entry_k + 1 + 500, n5)):
            hit_sl = (lows5[m] <= sl) if direction == "BUY" else (highs5[m] >= sl)
            hit_tp = (highs5[m] >= tp) if direction == "BUY" else (lows5[m] <= tp)
            if hit_sl:
                outcome = "LOSS"
                resolve_time = times5[m]
                resolve_minutes = (pd.Timestamp(resolve_time) - pd.Timestamp(times5[entry_k])).total_seconds() / 60
                break
            if hit_tp:
                outcome = "WIN"
                resolve_time = times5[m]
                resolve_minutes = (pd.Timestamp(resolve_time) - pd.Timestamp(times5[entry_k])).total_seconds() / 60
                break

        trades.append({
            "entry_time": times5[entry_k], "signal": direction, "entry": entry_price,
            "sl": sl, "tp": tp, "outcome": outcome, "resolve_min": resolve_minutes,
        })

        # IMPORTANT: don't look for a new setup until this trade is done.
        # Advance the 15min index past the resolution time (or, if it
        # never resolved within the lookahead window, past a cooldown)
        # so the same breakout/retest can't spawn overlapping signals.
        if resolve_time is not None:
            advance_to = resolve_time
        else:
            advance_to = times5[min(entry_k + 500, n5 - 1)]
        next_i = i
        for idx in range(i, n15):
            if times15[idx] >= advance_to:
                next_i = idx
                break
        else:
            next_i = n15
        i = max(next_i, retest_i + 1)

    return pd.DataFrame(trades)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="XAU/USD")
    parser.add_argument("--chunks", type=int, default=4)
    args = parser.parse_args()

    print(f"Fetching 4H data for {args.pair}...", flush=True)
    df4h = fetch_df(args.pair, "4h", max(1, args.chunks // 2))
    print(f"Fetching 15min data for {args.pair}...", flush=True)
    df15 = fetch_df(args.pair, "15min", args.chunks)
    print(f"Fetching 5min data for {args.pair}...", flush=True)
    df5 = fetch_df(args.pair, "5min", args.chunks)

    print(f"\n4H: {len(df4h)} bars | 15min: {len(df15)} bars | 5min: {len(df5)} bars", flush=True)

    trades = simulate(args.pair, df4h, df15, df5)

    if trades.empty:
        msg = f"\U0001F4CA *3-TF Breakout+Retest Backtest \u2014 {args.pair}*\nNo trades were generated over the tested period."
        print(msg)
        send_telegram(msg)
        return

    wins = (trades["outcome"] == "WIN").sum()
    losses = (trades["outcome"] == "LOSS").sum()
    still_open = (trades["outcome"] == "OPEN").sum()
    resolved = wins + losses
    win_rate = (wins / resolved * 100) if resolved else 0
    total_r = wins * RR_RATIO - losses * 1
    expectancy = (total_r / resolved) if resolved else 0

    resolved_trades = trades[trades["resolve_min"].notna()]
    avg_resolve = resolved_trades["resolve_min"].mean() if not resolved_trades.empty else 0
    under_15 = (resolved_trades["resolve_min"] < 15).sum() if not resolved_trades.empty else 0

    print("=" * 60)
    print(f"BACKTEST RESULTS: {args.pair} (4H bias / 15min setup / 5min entry)")
    print("=" * 60)
    print(f"Total signals fired:     {len(trades)}")
    print(f"Wins / Losses / Open:    {wins} / {losses} / {still_open}")
    print(f"Win rate (resolved):     {win_rate:.1f}%")
    print(f"Total R:                 {total_r:+.1f}R")
    print(f"Expectancy per trade:    {expectancy:+.2f}R")
    print(f"Avg time to resolve:     {avg_resolve:.0f} min")
    print(f"Trades resolved <15min:  {under_15} of {resolved}")
    print()
    print("Trade log:")
    for _, t in trades.iterrows():
        rm = f"{t['resolve_min']:.0f}min" if pd.notna(t["resolve_min"]) else "n/a"
        print(f"  {t['entry_time']}  {t['signal']:4s}  entry={t['entry']:.2f}  "
              f"sl={t['sl']:.2f}  tp={t['tp']:.2f}  -> {t['outcome']} ({rm})")

    summary = (
        f"\U0001F4CA *3-TF Breakout+Retest Backtest \u2014 {args.pair}*\n"
        f"4H bias, 15min zone(3+ touches)/breakout/retest, 5min entry, {RR_RATIO:.0f}R target\n\n"
        f"Total signals: `{len(trades)}`\n"
        f"Wins: `{wins}`  Losses: `{losses}`  Open: `{still_open}`\n"
        f"Win rate: `{win_rate:.1f}%`\n"
        f"Total R: `{total_r:+.1f}R`\n"
        f"Expectancy: `{expectancy:+.2f}R`\n"
        f"Avg resolve time: `{avg_resolve:.0f} min`\n"
        f"Resolved under 15min: `{under_15}/{resolved}`"
    )
    print("\nSending summary to Telegram...", flush=True)
    try:
        send_telegram(summary)
        print("Sent.")
    except Exception as e:
        print(f"Telegram send failed (results above still valid): {e}")


if __name__ == "__main__":
    main()
