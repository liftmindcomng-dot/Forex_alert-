"""
Backtests the SMC/ICT signal logic in forex_alert.py (sweep + MSS/CHoCH
+ required FVG or Order Block, on 5-minute candles) against a deep
window of real historical data from Twelve Data.

Unlike a single time_series call (capped around 5,000 bars), this pages
backward through multiple chunks using Twelve Data's `end_date` param,
so you can backtest months of 5-minute data instead of just the last
~17 days.

Reuses the SAME compute_signal() function your live bot calls, imported
directly from forex_alert.py — so the backtest and the live bot can
never quietly drift out of sync with each other.

SETUP:
    export TWELVE_DATA_API_KEY=your_real_key
    export TELEGRAM_BOT_TOKEN=dummy
    export TELEGRAM_CHAT_ID=dummy

USAGE:
    python backtest_smc.py --pair XAU/USD --interval 5min --chunks 6

  --chunks controls how far back you go: each chunk pulls up to 5,000
  bars, then the next chunk continues from where that one left off.
  6 chunks of 5-minute bars is roughly 100+ trading days, depending on
  market hours for the pair.
"""

import argparse
import json
import time
import urllib.request
import urllib.parse

from forex_alert import API_KEY, compute_signal, RR_RATIO, send_telegram

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


def fetch_history(pair, interval, chunks):
    """Pages backward through Twelve Data, oldest bars last, then we
    reverse everything once at the end so the arrays run oldest->newest
    just like forex_alert.py expects."""
    all_rows = []
    end_date = None
    for i in range(chunks):
        if i > 0:
            time.sleep(8)  # stay under the free-tier 8 req/min limit
        rows = fetch_chunk(pair, interval, CHUNK_SIZE, end_date)
        if not rows:
            break
        all_rows.extend(rows)
        end_date = rows[-1]["datetime"]
        print(f"  Chunk {i + 1}/{chunks}: {len(rows)} bars, back to {end_date}")

    # de-dupe (chunk boundaries can overlap by one bar) and sort oldest->newest
    seen = {}
    for r in all_rows:
        seen[r["datetime"]] = r
    rows_sorted = sorted(seen.values(), key=lambda r: r["datetime"])

    times = [r["datetime"] for r in rows_sorted]
    opens = [float(r["open"]) for r in rows_sorted]
    highs = [float(r["high"]) for r in rows_sorted]
    lows = [float(r["low"]) for r in rows_sorted]
    closes = [float(r["close"]) for r in rows_sorted]
    return times, opens, highs, lows, closes


def simulate_trade(highs, lows, entry_i, signal, sl, tp, max_lookahead=200):
    n = len(highs)
    for j in range(entry_i + 1, min(entry_i + 1 + max_lookahead, n)):
        if signal == "BUY":
            hit_sl = lows[j] <= sl
            hit_tp = highs[j] >= tp
        else:
            hit_sl = highs[j] >= sl
            hit_tp = lows[j] <= tp
        # Conservative: if both hit on the same bar, assume the worse
        # outcome (SL) since we don't know the intrabar order.
        if hit_sl:
            return "LOSS"
        if hit_tp:
            return "WIN"
    return "OPEN"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="XAU/USD")
    parser.add_argument("--interval", default="5min")
    parser.add_argument("--chunks", type=int, default=6)
    args = parser.parse_args()

    print(f"Fetching {args.chunks} chunk(s) of {args.pair} at {args.interval}...")
    times, opens, highs, lows, closes = fetch_history(args.pair, args.interval, args.chunks)
    print(f"\nTotal: {len(times)} bars, {times[0]} to {times[-1]}\n")

    trades = []
    min_history = 30
    last_alert_time = None

    for i in range(min_history, len(closes)):
        result = compute_signal(
            times[:i + 1], opens[:i + 1], highs[:i + 1], lows[:i + 1], closes[:i + 1]
        )

        if result["is_fresh_signal"] and result["signal"] in ("BUY", "SELL"):
            if result["bar_time"] == last_alert_time:
                continue
            last_alert_time = result["bar_time"]

            outcome = simulate_trade(highs, lows, i, result["signal"], result["sl"], result["tp"])
            trades.append({
                "time": result["bar_time"],
                "signal": result["signal"],
                "entry": result["price"],
                "sl": result["sl"],
                "tp": result["tp"],
                "outcome": outcome,
            })

    wins = sum(1 for t in trades if t["outcome"] == "WIN")
    losses = sum(1 for t in trades if t["outcome"] == "LOSS")
    still_open = sum(1 for t in trades if t["outcome"] == "OPEN")
    resolved = wins + losses
    win_rate = (wins / resolved * 100) if resolved else 0
    total_r = wins * RR_RATIO - losses * 1
    expectancy = (total_r / resolved) if resolved else 0

    print("=" * 55)
    print(f"BACKTEST RESULTS: {args.pair} {args.interval} (sweep + MSS/CHoCH + FVG-or-OB)")
    print("=" * 55)
    print(f"Total signals fired:     {len(trades)}")
    print(f"Wins / Losses / Open:    {wins} / {losses} / {still_open}")
    print(f"Win rate (resolved):     {win_rate:.1f}%")
    print(f"Total R (win=+{RR_RATIO}R, loss=-1R): {total_r:+.1f}R")
    print(f"Expectancy per trade:    {expectancy:+.2f}R")
    print()
    print("Trade log:")
    for t in trades:
        print(f"  {t['time']}  {t['signal']:4s}  entry={t['entry']:.2f}  "
              f"sl={t['sl']:.2f}  tp={t['tp']:.2f}  -> {t['outcome']}")

    span_days = None
    try:
        from datetime import datetime
        fmt = "%Y-%m-%d %H:%M:%S"
        span_days = (datetime.strptime(times[-1], fmt) - datetime.strptime(times[0], fmt)).days
    except Exception:
        pass

    summary = (
        f"\U0001F4CA *SMC Backtest \u2014 {args.pair}*\n"
        f"5-min bars, sweep + MSS/CHoCH + required FVG-or-OB, fixed {RR_RATIO:.0f}R target\n"
        f"No higher-timeframe bias filter.\n\n"
        f"Period tested: {times[0]} to {times[-1]}"
        + (f" (~{span_days} days)" if span_days is not None else "") + "\n"
        f"Total signals: `{len(trades)}`\n"
        f"Wins: `{wins}`  Losses: `{losses}`  Still open: `{still_open}`\n"
        f"Win rate: `{win_rate:.1f}%`\n"
        f"Total R: `{total_r:+.1f}R`\n"
        f"Expectancy per trade: `{expectancy:+.2f}R`"
    )
    print("\nSending summary to Telegram...")
    try:
        send_telegram(summary)
        print("Sent.")
    except Exception as e:
        print(f"Telegram send failed (results above are still valid): {e}")


if __name__ == "__main__":
    main()
