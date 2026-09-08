"""
backtest.py
Walk-forward backtest that replicates forex_alert.py's ENTRY_MODE=structure
pipeline exactly, using the real functions it defines (there is no single
evaluate_swing_signal() in that file - the logic lives inline inside
process_pair(), built from check_structure_break, find_order_blocks,
find_supply_demand_zone, find_liquidity_pools, liquidity_swept_before_break,
count_level_touches, check_smc_confirmation, build_condition_label,
classify_conviction, sync_order_blocks, active_zones_for, and
update_order_block_mitigation).

Importing forex_alert.py executes its top-level code, which requires
TWELVE_DATA_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID to exist as env
vars (even though this script never calls Telegram) - backtest.yml sets
dummy values for the Telegram ones.

Outputs:
  - backtest_trades.csv
  - backtest_summary.json
"""

import os
import sys
import csv
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

import forex_alert as fa

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
PAIR = os.environ.get("BACKTEST_PAIR", "XAU/USD")

TF_TREND = os.environ.get("TF_TREND", fa.TF_TREND)
TF_STRUCTURE = os.environ.get("TF_STRUCTURE", fa.TF_STRUCTURE)
TF_ENTRY = os.environ.get("TF_ENTRY", fa.TF_ENTRY)

START_DATE = os.environ.get("BACKTEST_START", "2024-01-01")
END_DATE = os.environ.get("BACKTEST_END", datetime.now(timezone.utc).strftime("%Y-%m-%d"))

API_CALL_SLEEP = float(os.environ.get("API_CALL_SLEEP_SECONDS", "8"))

TRADES_CSV = os.environ.get("TRADES_CSV", "backtest_trades.csv")
SUMMARY_JSON = os.environ.get("SUMMARY_JSON", "backtest_summary.json")

DURATION_LIMITS = {
    "day": fa.DAY_MAX_HOLD_HOURS,
    "day_swing": fa.DAY_SWING_MAX_HOLD_HOURS,
    "swing": fa.SWING_MAX_HOLD_HOURS,
}


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
                print("Rate limit hit - stopping pagination early with " + str(len(all_rows)) + " rows so far.")
                break
            raise RuntimeError("Twelve Data error [" + pair + " " + interval + "]: " + str(msg))

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

    times = [r["datetime"] for r in ordered]
    opens = [float(r["open"]) for r in ordered]
    highs = [float(r["high"]) for r in ordered]
    lows = [float(r["low"]) for r in ordered]
    closes = [float(r["close"]) for r in ordered]
    return times, opens, highs, lows, closes


class OpenTrade(object):
    def __init__(self, direction, entry, sl, duration_class, condition_label, opened_at):
        self.direction = direction  # "bullish" / "bearish"
        self.entry = entry
        self.sl = sl
        self.r = abs(entry - sl)
        self.duration_class = duration_class
        self.condition_label = condition_label
        self.opened_at = opened_at

        tp_prices = []
        for m in fa.TP_MULTIPLES:
            if direction == "bullish":
                tp_prices.append(entry + m * self.r)
            else:
                tp_prices.append(entry - m * self.r)
        self.tp_prices = tp_prices
        self.tp_hit = [False] * len(tp_prices)
        self.tp_fraction = 1.0 / len(tp_prices)
        self.remaining_fraction = 1.0

        self.realized_r = 0.0
        self.closed = False
        self.close_reason = None
        self.close_time = None

    def max_hold_hours(self):
        return DURATION_LIMITS.get(self.duration_class)

    def process_bar(self, o, h, l, c, bar_time):
        if self.closed:
            return

        opened_dt = datetime.strptime(self.opened_at, "%Y-%m-%d %H:%M:%S")
        now_dt = datetime.strptime(bar_time, "%Y-%m-%d %H:%M:%S")
        hours_open = (now_dt - opened_dt).total_seconds() / 3600

        if self.direction == "bullish":
            sl_hit = l <= self.sl
        else:
            sl_hit = h >= self.sl

        if sl_hit:
            self.realized_r += self.remaining_fraction * (-1.0)
            self.remaining_fraction = 0.0
            self.closed = True
            self.close_reason = "SL"
            self.close_time = bar_time
            return

        for i in range(len(self.tp_prices)):
            if self.tp_hit[i]:
                continue
            tp_price = self.tp_prices[i]
            if self.direction == "bullish":
                hit = h >= tp_price
            else:
                hit = l <= tp_price
            if hit:
                self.tp_hit[i] = True
                self.realized_r += self.tp_fraction * fa.TP_MULTIPLES[i]
                self.remaining_fraction -= self.tp_fraction

        if all(self.tp_hit):
            self.closed = True
            self.close_reason = "ALL_TP"
            self.close_time = bar_time
            return

        limit_hours = self.max_hold_hours()
        if limit_hours is not None and hours_open >= limit_hours:
            if self.direction == "bullish":
                mtm_r = (c - self.entry) / self.r
            else:
                mtm_r = (self.entry - c) / self.r
            self.realized_r += self.remaining_fraction * mtm_r
            self.remaining_fraction = 0.0
            self.closed = True
            self.close_reason = "TIME_STOP"
            self.close_time = bar_time

    def to_row(self):
        return {
            "direction": self.direction,
            "entry": self.entry,
            "sl": self.sl,
            "r_distance": self.r,
            "duration_class": self.duration_class,
            "condition_label": self.condition_label,
            "opened_at": self.opened_at,
            "closed_at": self.close_time,
            "close_reason": self.close_reason,
            "tp_levels_hit": sum(self.tp_hit),
            "realized_r": round(self.realized_r, 3),
        }


def run_backtest():
    print("Fetching " + TF_TREND + " candles for " + PAIR + " (" + START_DATE + " to " + END_DATE + ")...")
    t4, o4, h4, l4, c4 = fetch_historical(PAIR, TF_TREND, START_DATE, END_DATE)
    time.sleep(API_CALL_SLEEP)

    print("Fetching " + TF_STRUCTURE + " candles for " + PAIR + "...")
    ts, os_, hs, ls, cs = fetch_historical(PAIR, TF_STRUCTURE, START_DATE, END_DATE)
    time.sleep(API_CALL_SLEEP)

    print("Fetching " + TF_ENTRY + " candles for " + PAIR + "...")
    te, oe, he, le, ce = fetch_historical(PAIR, TF_ENTRY, START_DATE, END_DATE)

    print("Loaded: " + str(len(t4)) + " " + TF_TREND + " bars, " +
          str(len(ts)) + " " + TF_STRUCTURE + " bars, " +
          str(len(te)) + " " + TF_ENTRY + " bars.")

    if len(te) < 100:
        print("Not enough entry-TF data to backtest meaningfully.")
        return

    pair_state = {"order_blocks": [], "bias": None}
    setup = None
    open_trade = None
    closed_trades = []

    trend_idx = 0
    structure_idx = 0
    last_structure_idx_checked = -1
    cached_bos = None
    cached_zones, cached_liquidity_ok, cached_sr_ok = [], True, True

    for i in range(30, len(te)):
        bar_time = te[i]

        while trend_idx < len(t4) - 1 and t4[trend_idx + 1] <= bar_time:
            trend_idx += 1
        while structure_idx < len(ts) - 1 and ts[structure_idx + 1] <= bar_time:
            structure_idx += 1

        if trend_idx < 20 or structure_idx < 20:
            continue

        if open_trade:
            open_trade.process_bar(oe[i], he[i], le[i], ce[i], bar_time)
            if open_trade.closed:
                closed_trades.append(open_trade.to_row())
                open_trade = None

        if open_trade:
            continue

        bias = fa.get_bias(h4[:trend_idx + 1], l4[:trend_idx + 1])
        if bias is None:
            continue

        bias_flipped = pair_state.get("bias") is not None and pair_state["bias"] != bias
        if bias_flipped:
            setup = None
        pair_state["bias"] = bias

        if structure_idx != last_structure_idx_checked or bias_flipped:
            hs_w = hs[:structure_idx + 1]
            ls_w = ls[:structure_idx + 1]
            cs_w = cs[:structure_idx + 1]
            os_w = os_[:structure_idx + 1]
            atr_s = fa.atr(hs_w, ls_w, cs_w, 14)
            bos = fa.check_structure_break(
                hs_w, ls_w, cs_w, bias,
                displacement_atr_mult=fa.DISPLACEMENT_ATR_MULT, atr_val=atr_s,
            )
            zones, liquidity_ok, sr_ok = [], True, True
            if bos:
                bos_level, pullback_zone, bos_index = bos
                zones = fa.find_order_blocks(os_w, hs_w, ls_w, cs_w, bias, bos_index, fa.OB_LOOKBACK, fa.OB_MAX_ZONES)
                if len(zones) < fa.OB_MAX_ZONES:
                    sd_zone = fa.find_supply_demand_zone(
                        os_w, hs_w, ls_w, cs_w, bias, bos_index, atr_s,
                        fa.SD_CONSOLIDATION_BARS, fa.SD_MOVE_ATR_MULT,
                    )
                    if sd_zone:
                        zones.append(sd_zone)
                if fa.LIQUIDITY_LOOKBACK > 0:
                    tol = fa.SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
                    swings_s = fa.find_swings(hs_w, ls_w, fa.SWING_LOOKBACK)
                    pools = fa.find_liquidity_pools(swings_s, tol)
                    liquidity_ok = fa.liquidity_swept_before_break(pools, bias, bos_index, fa.LIQUIDITY_LOOKBACK)
                if fa.SR_MIN_TOUCHES > 0:
                    if zones:
                        level = zones[0]["low"] if bias == "bullish" else zones[0]["high"]
                        tol = fa.SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
                        touches = fa.count_level_touches(hs_w, ls_w, level, tol, len(hs_w), bos_index)
                        sr_ok = touches >= fa.SR_MIN_TOUCHES
                    else:
                        sr_ok = False

                fa.sync_order_blocks(pair_state, bias, zones, bias_flipped, fa.OB_MAX_ZONES)
                fa.update_order_block_mitigation(pair_state.get("order_blocks", []), cs_w[-1])

            last_structure_idx_checked = structure_idx
            cached_bos, cached_zones, cached_liquidity_ok, cached_sr_ok = bos, zones, liquidity_ok, sr_ok
        else:
            bos, zones, liquidity_ok, sr_ok = cached_bos, cached_zones, cached_liquidity_ok, cached_sr_ok

        if bos:
            bos_level, pullback_zone, bos_index = bos
            if not setup or setup.get("bos_level") != bos_level:
                setup = {
                    "bos_level": bos_level, "pullback_zone": pullback_zone, "confirmed": False,
                    "zones": zones, "liquidity_ok": liquidity_ok, "sr_ok": sr_ok, "is_choch": bias_flipped,
                }

        if not setup or setup.get("confirmed"):
            continue
        if not setup.get("zones"):
            continue
        if not setup.get("liquidity_ok", True):
            continue
        if not setup.get("sr_ok", True):
            continue

        he_w = he[max(0, i - 50):i + 1]
        le_w = le[max(0, i - 50):i + 1]
        ce_w = ce[max(0, i - 50):i + 1]
        oe_w = oe[max(0, i - 50):i + 1]
        te_w = te[max(0, i - 50):i + 1]
        a5 = fa.atr(he_w, le_w, ce_w, 14) or 0

        fa.update_order_block_mitigation(pair_state.get("order_blocks", []), ce_w[-1])
        active_zones = fa.active_zones_for(pair_state, bias)
        if not active_zones:
            continue

        confirmation = fa.check_smc_confirmation(
            te_w, oe_w, he_w, le_w, ce_w, bias, active_zones,
            fa.SESSION_START_UTC, fa.SESSION_END_UTC,
        )
        if not confirmation:
            continue

        confs = confirmation.get("confirmations", {})
        full_confirmations = {
            "fvg": confs.get("fvg", False),
            "liquidity": setup.get("liquidity_ok", False),
            "displacement": True,
            "rejection": confs.get("rejection", False),
        }
        sr_hit = fa.SR_MIN_TOUCHES > 0 and setup.get("sr_ok", False)
        sd_hit = confirmation.get("zone_type") in ("demand_zone", "supply_zone")
        is_choch = setup.get("is_choch", False)
        condition_label = fa.build_condition_label(is_choch, full_confirmations, sr_hit, sd_hit)
        duration_class, duration_note = fa.classify_conviction(is_choch, full_confirmations, sr_hit, sd_hit)

        entry_price = confirmation["entry"]
        buffer = fa.SL_BUFFER_ATR_MULT * a5
        if bias == "bullish":
            sl = confirmation["sl_anchor"] - buffer
        else:
            sl = confirmation["sl_anchor"] + buffer
        r = abs(entry_price - sl)

        setup["confirmed"] = True

        if r <= 0:
            continue

        open_trade = OpenTrade(bias, entry_price, sl, duration_class, condition_label, bar_time)

    total = len(closed_trades)
    wins = [t for t in closed_trades if t["realized_r"] > 0]

    if total > 0:
        win_rate = round(len(wins) / total * 100, 1)
        avg_r = round(sum(t["realized_r"] for t in closed_trades) / total, 3)
    else:
        win_rate = 0.0
        avg_r = 0.0

    total_r = round(sum(t["realized_r"] for t in closed_trades), 2)

    max_consec_losses = 0
    cur_consec = 0
    running_r = 0.0
    peak_r = 0.0
    max_drawdown_r = 0.0
    for t in closed_trades:
        if t["realized_r"] <= 0:
            cur_consec += 1
            if cur_consec > max_consec_losses:
                max_consec_losses = cur_consec
        else:
            cur_consec = 0
        running_r += t["realized_r"]
        if running_r > peak_r:
            peak_r = running_r
        drawdown = running_r - peak_r
        if drawdown < max_drawdown_r:
            max_drawdown_r = drawdown

    by_reason = {}
    for t in closed_trades:
        reason = t["close_reason"]
        by_reason[reason] = by_reason.get(reason, 0) + 1

    by_condition = {}
    for t in closed_trades:
        key = t["condition_label"]
        if key not in by_condition:
            by_condition[key] = {"count": 0, "total_r": 0.0}
        by_condition[key]["count"] += 1
        by_condition[key]["total_r"] += t["realized_r"]

    performance_by_condition = {}
    for k, v in by_condition.items():
        performance_by_condition[k] = {
            "count": v["count"],
            "avg_r": round(v["total_r"] / v["count"], 3),
        }

    summary = {
        "pair": PAIR,
        "period": START_DATE + " to " + END_DATE,
        "total_trades": total,
        "win_rate_pct": win_rate,
        "avg_r_per_trade": avg_r,
        "total_r": total_r,
        "max_consecutive_losses": max_consec_losses,
        "max_drawdown_r": round(max_drawdown_r, 2),
        "close_reason_breakdown": by_reason,
        "performance_by_condition_label": performance_by_condition,
    }

    with open(SUMMARY_JSON, "w") as f:
        json.dump(summary, f, indent=2)

    if closed_trades:
        with open(TRADES_CSV, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(closed_trades[0].keys()))
            writer.writeheader()
            writer.writerows(closed_trades)

    print(json.dumps(summary, indent=2))
    print("")
    print("Wrote " + TRADES_CSV + " and " + SUMMARY_JSON + ".")


if __name__ == "__main__":
    try:
        run_backtest()
    except Exception as e:
        print("ERROR: " + str(e), file=sys.stderr)
        sys.exit(1)
