"""
backtest.py
Walk-forward backtest for evaluate_swing_signal() (now living inside
forex_alert.py after merging smc_engine_v2 + time_stop into it), using
real historical data pulled from Twelve Data (paginated, since a single
call is capped at outputsize=5000).

NOTE: importing from forex_alert.py executes its top-level code, which
requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID env vars to exist (even
though this script never sends Telegram messages) - backtest.yml sets
dummy values for those so the import doesn't crash.

Simulates partial exits: each TP level (from TP_MULTIPLES) closes an
equal fraction of the position (100/len(TP_MULTIPLES) percent each).
If SL is hit, whatever fraction of the position remains open closes at
-1R. If DAY_MAX_HOLD_HOURS/etc. is exceeded before SL or the final TP,
the remaining fraction closes at current mark-to-market R.

Outputs:
  - backtest_trades.csv   (one row per closed trade)
  - backtest_summary.json (aggregate stats)

This does NOT call Telegram, MetaApi, or touch state.json for live
running - it's a standalone historical simulation only.
"""

import os
import sys
import csv
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

from forex_alert import evaluate_swing_signal, atr, DAY_MAX_HOLD_HOURS, DAY_SWING_MAX_HOLD_HOURS, SWING_MAX_HOLD_HOURS

# ==================== CONFIG ====================
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
PAIR = os.environ.get("BACKTEST_PAIR", "XAU/USD")

TF_TREND = os.environ.get("TF_TREND", "4h")
TF_STRUCTURE = os.environ.get("TF_STRUCTURE", "1h")
TF_ENTRY = os.environ.get("TF_ENTRY", "15min")

START_DATE = os.environ.get("BACKTEST_START", "2024-01-01")
END_DATE = os.environ.get("BACKTEST_END", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

SWING_LOOKBACK = int(os.environ.get("SWING_LOOKBACK", "2"))
SL_BUFFER_ATR_MULT = float(os.environ.get("SL_BUFFER_ATR_MULT", "0.15"))
OB_MAX_ZONES = int(os.environ.get("OB_MAX_ZONES", "5"))
LIQUIDITY_LOOKBACK = int(os.environ.get("LIQUIDITY_LOOKBACK", "10"))
DISPLACEMENT_ATR_MULT = float(os.environ.get("DISPLACEMENT_ATR_MULT", "1.3"))
SMC_REJECTION_WICK_RATIO = float(os.environ.get("SMC_REJECTION_WICK_RATIO", "2.0"))
SR_MIN_TOUCHES = int(os.environ.get("SR_MIN_TOUCHES", "3"))

TP_MULTIPLES = tuple(float(x) for x in os.environ.get("TP_MULTIPLES", "1,2,3,4,5").split(",") if x.strip())
TP_FRACTION = 1.0 / len(TP_MULTIPLES)

SESSION_START_UTC = int(os.environ.get("SESSION_START_UTC", "7"))
SESSION_END_UTC = int(os.environ.get("SESSION_END_UTC", "21"))

API_CALL_SLEEP = float(os.environ.get("API_CALL_SLEEP_SECONDS", "8"))

TRADES_CSV = os.environ.get("TRADES_CSV", "backtest_trades.csv")
SUMMARY_JSON = os.environ.get("SUMMARY_JSON", "backtest_summary.json")


# ==================== PAGINATED HISTORICAL FETCH ====================
def fetch_historical(pair, interval, start_date, end_date, max_per_call=5000):
    all_rows = []
    current_end = end_date

    while True:
        url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
            "symbol": pair,
            "interval": interval,
            "start_date": start_date,
            "end_date": current_end,
            "outputsize": max_per_call,
            "apikey": API_KEY,
            "timezone": "UTC",
        })
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())

        if "values" not in data:
            msg = data.get("message", data)
            if "run out of API credits" in str(msg).lower() or data.get("code") == 429:
                print(f"Rate limit hit — stopping pagination early with {len(all_rows)} rows so far.")
                break
            raise RuntimeError(f"Twelve Data error [{pair} {interval}]: {msg}")

        rows = data["values"]
        if not rows:
            break

        all_rows.extend(rows)

        oldest_in_batch = rows[-1]["datetime"]
        if len(rows) < max_per_call or oldest_in_batch <= start_date:
            break

        oldest_dt = datetime.strptime(oldest_in_batch, "%Y-%m-%d %H:%M:%S")
        current_end = (oldest_dt - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")
        time.sleep(API_CALL_SLEEP)

    seen = {}
    for r in all_rows:
        seen[r["datetime"]] = r
    ordered = sorted(seen.values(), key=lambda r: r["datetime"])

    return [
        {"time": r["datetime"], "open": float(r["open"]), "high": float(r["high"]),
         "low": float(r["low"]), "close": float(r["close"])}
        for r in ordered
    ]


def in_session(iso_time, start_hour, end_hour):
    try:
        hour = int(iso_time[11:13])
    except (IndexError, ValueError):
        return True
    if start_hour <= end_hour:
        return start_hour <= hour < end_hour
    return hour >= start_hour or hour < end_hour


# ==================== TRADE SIMULATION ====================
class OpenTrade:
    def __init__(self, signal, opened_at, opened_index):
        self.direction = signal["direction"]
        self.entry = signal["entry"]
        self.sl = signal["sl"]
        self.r = abs(self.entry - self.sl)
        self.duration_class = signal["duration"]
        self.condition_label = signal["condition_label"]
        self.opened_at = opened_at
        self.opened_index = opened_index
        self.tp_prices = [
            self.entry + m * self.r if self.direction == "bull" else self.entry - m * self.r
            for m in TP_MULTIPLES
        ]
        self.tp_hit = [False] * len(self.tp_prices)
        self.remaining_fraction = 1.0
        self.realized_r = 0.0
        self.closed = False
        self.close_reason = None
        self.close_time = None

    def max_hold_hours(self):
        return {"DAY": DAY_MAX_HOLD_HOURS, "DAY/SWING": DAY_SWING_MAX_HOLD_HOURS,
                "SWING": SWING_MAX_HOLD_HOURS}.get(self.duration_class, DAY_MAX_HOLD_HOURS)

    def process_bar(self, candle, bar_time):
        if self.closed:
            return

        hours_open = (datetime.strptime(bar_time, "%Y-%m-%d %H:%M:%S") -
                      datetime.strptime(self.opened_at, "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600

        sl_hit = (candle["low"] <= self.sl) if self.direction == "bull" else (candle["high"] >= self.sl)
        if sl_hit:
            self.realized_r += self.remaining_fraction * (-1.0)
            self.remaining_fraction = 0.0
            self.closed = True
            self.close_reason = "SL"
            self.close_time = bar_time
            return

        for i, tp_price in enumerate(self.tp_prices):
            if self.tp_hit[i]:
                continue
            hit = (candle["high"] >= tp_price) if self.direction == "bull" else (candle["low"] <= tp_price)
            if hit:
                self.tp_hit[i] = True
                self.realized_r += TP_FRACTION * TP_MULTIPLES[i]
                self.remaining_fraction -= TP_FRACTION

        if all(self.tp_hit):
            self.closed = True
            self.close_reason = "ALL_TP"
            self.close_time = bar_time
            return

        if hours_open >= self.max_hold_hours():
            mtm_r = (candle["close"] - self.entry) / self.r if self.direction == "bull" else (self.entry - candle["close"]) / self.r
            self.realized_r += self.remaining_fraction * mtm_r
            self.remaining_fraction = 0.0
            self.closed = True
            self.close_reason = "TIME_STOP"
            self.close_time = bar_time

    def to_row(self):
        return {
            "direction": self.direction, "entry": self.entry, "sl": self.sl, "r_distance": self.r,
            "duration_class": self.duration_class, "condition_label": self.condition_label,
            "opened_at": self.opened_at, "closed_at": self.close_time, "close_reason": self.close_reason,
            "tp_levels_hit": sum(self.tp_hit), "realized_r": round(self.realized_r, 3),
        }


# ==================== MAIN BACKTEST LOOP ====================
def run_backtest():
    print(f"Fetching {TF_TREND} candles for {PAIR} ({START_DATE} to {END_DATE})...")
    trend_all = fetch_historical(PAIR, TF_TREND, START_DATE, END_DATE)
    time.sleep(API_CALL_SLEEP)
    print(f"Fetching {TF_STRUCTURE} candles for {PAIR}...")
    structure_all = fetch_historical(PAIR, TF_STRUCTURE, START_DATE, END_DATE)
    time.sleep(API_CALL_SLEEP)
    print(f"Fetching {TF_ENTRY} candles for {PAIR}...")
    entry_all = fetch_historical(PAIR, TF_ENTRY, START_DATE, END_DATE)

    print(f"Loaded: {len(trend_all)} {TF_TREND} bars, {len(structure_all)} {TF_STRUCTURE} bars, {len(entry_all)} {TF_ENTRY} bars.")

    if len(entry_all) < 100:
        print("Not enough entry-TF data to backtest meaningfully — check date range / API limits.")
        return

    pair_state = {"trend": "none", "order_blocks": []}
    open_trade = None
    closed_trades = []

    trend_idx = 0
    structure_idx = 0

    for i in range(20, len(entry_all)):
        bar_time = entry_all[i]["time"]

        while trend_idx < len(trend_all) - 1 and trend_all[trend_idx + 1]["time"] <= bar_time:
            trend_idx += 1
        while structure_idx < len(structure_all) - 1 and structure_all[structure_idx + 1]["time"] <= bar_time:
            structure_idx += 1

        trend_window = trend_all[:trend_idx + 1]
        structure_window = structure_all[:structure_idx + 1]
        entry_window = entry_all[max(0, i - 30):i + 1]

        if len(trend_window) < 20 or len(structure_window) < 20:
            continue

        if open_trade:
            open_trade.process_bar(entry_all[i], bar_time)
            if open_trade.closed:
                closed_trades.append(open_trade.to_row())
                open_trade = None

        if open_trade:
            continue

        if not in_session(bar_time, SESSION_START_UTC, SESSION_END_UTC):
            continue

        signal = evaluate_swing_signal(
            trend_candles=trend_window, structure_candles=structure_window, entry_candles=entry_window,
            pair_state=pair_state, swing_lookback=SWING_LOOKBACK, ob_max_zones=OB_MAX_ZONES,
            liquidity_lookback=LIQUIDITY_LOOKBACK, displacement_atr_mult=DISPLACEMENT_ATR_MULT,
            rejection_wick_ratio=SMC_REJECTION_WICK_RATIO, sl_buffer_atr_mult=SL_BUFFER_ATR_MULT,
            sr_min_touches=SR_MIN_TOUCHES,
        )

        if signal:
            open_trade = OpenTrade(signal, bar_time, i)

    total = len(closed_trades)
    wins = [t for t in closed_trades if t["realized_r"] > 0]
    win_rate = round(len(wins) / total * 100, 1) if total else 0.0
    avg_r = round(sum(t["realized_r"] for t in closed_trades) / total, 3) if total else 0.0
    total_r = round(sum(t["realized_r"] for t in closed_trades), 2)

    max_consec_losses = cur_consec = 0
    running_r = 0.0
    peak_r = 0.0
    max_drawdown_r = 0.0
    for t in closed_trades:
        if t["realized_r"] <= 0:
            cur_consec += 1
            max_consec_losses = max(max_consec_losses, cur_consec)
        else:
            cur_consec = 0
        running_r += t["realized_r"]
        peak_r = max(peak_r, running_r)
        max_drawdown_r = min(max_drawdown_r, running_r - peak_r)

    by_reason = {}
    for t in closed_trades:
        by_reason[t["close_reason"]] = by_reason.get(t["close_reason"], 0) + 1

    by_condition = {}
    for t in closed_trades:
        key = t["condition_label"]
        by_condition.setdefault(key, {"count": 0, "total_r": 0.0})
        by_condition[key]["count"] += 1
        by_condition[key]["total_r"] += t["realized_r"]

    summary = {
        "pair": PAIR, "period": f"{START_DATE} to {END_DATE}",
        "total_trades": total, "win_rate_pct": win_rate,
        "avg_r_per_trade": avg_r, "total_r": total_r,
        "max_consecutive_losses": max_consec_losses, "max_drawdown_r": round(max_drawdown_r, 2),
        "close_reason_breakdown": by_reason,
        "performance_by_condition_label": {
            k: {"count": v["count"], "avg_r": round(v["total_r"] / v["count"], 3)}
            for k, v in by_condition.items()
        },
    }

    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2)

    if closed_trades:
        with open(TRADES_CSV, "w", newline="") as f:

