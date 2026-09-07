"""
Historical backtest for the forex signal bot.

Reuses the *exact same* detection logic as forex_alert.py (imported as a
module, not re-implemented) — bias, structure break, persistent order-
block/supply-demand zones with mitigation tracking, CHoCH/BOS tagging,
liquidity sweep, S/R confluence, condition labels, conviction scoring,
and all ENTRY_MODE dispatchers (retest / structure / retest_or_pullback
/ pullback). This guarantees the backtest can't silently drift from what
the live script actually does — change forex_alert.py, and the backtest
picks it up automatically on the next run, since it calls the same
functions rather than reimplementing them.

IMPORTANT: this file only ever calls functions that exist in
forex_alert.py as of the version currently in the repo. If forex_alert.py
is refactored (function renamed/removed), update the corresponding call
here — a mismatch will surface immediately as an ImportError or
AttributeError at the top of a run, which is exactly what happened with
an earlier version of this file that referenced a function
('evaluate_swing_signal') that was never actually defined in
forex_alert.py.

Config is read from the SAME env vars as forex_alert.py (FX_PAIRS,
TF_TREND, TF_STRUCTURE, TF_ENTRY, ENTRY_MODE, all the SMC/structure
knobs, TP_MULTIPLES, etc) — set the same env block your live workflow
uses, plus:

  BACKTEST_START_DATE / BACKTEST_END_DATE   "YYYY-MM-DD" (UTC). If unset,
                                             defaults to the last
                                             BACKTEST_DAYS days.
  BACKTEST_DAYS                             default 14. Only used if the
                                             explicit start/end aren't set.
  BACKTEST_SEND_TELEGRAM_SUMMARY            "true" to send a one-line
                                             result summary to Telegram
                                             at the end (off by default).

Writes backtest_results.json (full trade log + summary stats) next to
this script.

SIMPLIFYING ASSUMPTIONS (read before trusting the numbers):

  - "Bar closed" is approximated as "bar open-time <= current entry-bar
    time" — the bar's own duration isn't accounted for.

  - The live script's TREND_CACHE_MINUTES / STRUCTURE_CACHE_MINUTES
    caching is emulated by re-evaluating bias/structure only that often
    (by elapsed bar time), not on every single entry-timeframe bar —
    otherwise the backtest would "see" changes faster than the live cron
    schedule ever could, inflating signal count.

  - Persistent order-block tracking mirrors the live script exactly:
    zones found at each structure recompute are pushed into a running
    per-pair, per-direction store via sync_order_blocks, mitigation is
    checked at both the structure-timeframe close and the entry-
    timeframe close via update_order_block_mitigation, and entry
    confirmation only ever looks at active_zones_for(...) — the same
    functions forex_alert.py uses live.

  - Primary win/loss is decided by SL vs TP1 ONLY — this mirrors the
    live script's MetaApi auto-order, which only ever sets a single TP.
    TP2-5 are reported separately as "how far did it run" stats and do
    NOT affect the win/loss classification or R total.

  - If both SL and TP1 fall inside the same bar's high/low range, SL is
    assumed to have hit first (OHLC data alone can't tell you the true
    intrabar order).

  - No spread, slippage, commission, or swap is modeled. No position
    sizing or concurrent-trade limits either — every signal is scored as
    an independent, single, fixed-risk trade.

  - History is fetched from Twelve Data with a bounded number of
    pagination requests per (pair, timeframe) — see MAX_PAGES. A long
    backtest window on a low timeframe can exceed that and get
    truncated; a warning prints when it does.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.parse
from datetime import datetime, timedelta, timezone

MAX_PAGES = int(os.environ.get("BACKTEST_MAX_PAGES", "5"))
WARMUP_BARS = int(os.environ.get("BACKTEST_WARMUP_BARS", "30"))


def parse_bar_time(s):
    """Twelve Data returns 'YYYY-MM-DD HH:MM:SS' when timezone=UTC is
    requested. Parsed explicitly rather than via fromisoformat so
    behavior doesn't depend on the exact Python version's leniency."""
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def fetch_historical(pair, interval, start_date, end_date, api_key, sleep_seconds, outputsize=5000):
    """Fetch every bar for `pair`/`interval` between start_date and
    end_date (UTC, 'YYYY-MM-DD'), paginating backward in time when the
    range doesn't fit in one call."""
    all_rows = []
    cursor_end = end_date
    truncated = False
    for page in range(MAX_PAGES):
        url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
            "symbol": pair,
            "interval": interval,
            "start_date": start_date,
            "end_date": cursor_end,
            "outputsize": outputsize,
            "apikey": api_key,
            "timezone": "UTC",
        })
        with urllib.request.urlopen(url, timeout=30) as resp:
            data = json.loads(resp.read().decode())
        if "values" not in data:
            raise RuntimeError(f"Twelve Data error [{pair} {interval}]: {data.get('message', data)}")
        rows = data["values"]
        if not rows:
            break
        all_rows.extend(rows)
        earliest = rows[-1]["datetime"]
        if len(rows) < outputsize or earliest <= start_date:
            break
        cursor_end = earliest
        time.sleep(sleep_seconds)
    else:
        truncated = True

    if truncated and all_rows:
        print(f"[{pair} {interval}] Hit MAX_PAGES={MAX_PAGES} while paginating — "
              f"history may be truncated before {all_rows[-1]['datetime']}.")

    seen = set()
    unique_rows = []
    for r in all_rows:
        if r["datetime"] in seen:
            continue
        seen.add(r["datetime"])
        unique_rows.append(r)
    unique_rows.sort(key=lambda r: r["datetime"])

    times = [r["datetime"] for r in unique_rows]
    opens = [float(r["open"]) for r in unique_rows]
    highs = [float(r["high"]) for r in unique_rows]
    lows = [float(r["low"]) for r in unique_rows]
    closes = [float(r["close"]) for r in unique_rows]
    return times, opens, highs, lows, closes


def advance_closed_index(times_list, t, start_ptr):
    idx = start_ptr
    n = len(times_list)
    while idx + 1 < n and times_list[idx + 1] <= t:
        idx += 1
    return idx


def simulate_trade_outcome(highs, lows, bias, start_index, sl, tps):
    """Walk forward from `start_index` to find whether SL or TP1 hits
    first (the win/loss result). Also tracks the furthest TP level ever
    touched, informational only."""
    n = len(highs)
    tp1 = tps[0]
    max_tp_reached = 0
    for i in range(start_index, n):
        if bias == "bullish":
            sl_hit = lows[i] <= sl
            tp1_hit = highs[i] >= tp1
            for idx, tp in enumerate(tps, start=1):
                if highs[i] >= tp:
                    max_tp_reached = max(max_tp_reached, idx)
        else:
            sl_hit = highs[i] >= sl
            tp1_hit = lows[i] <= tp1
            for idx, tp in enumerate(tps, start=1):
                if lows[i] <= tp:
                    max_tp_reached = max(max_tp_reached, idx)

        if sl_hit:
            return {"outcome": "loss", "r_result": -1.0,
                    "bars_held": i - start_index + 1, "max_tp_reached": max_tp_reached}
        if tp1_hit:
            return {"outcome": "win", "r_result": 1.0,
                    "bars_held": i - start_index + 1, "max_tp_reached": max_tp_reached}

    return {"outcome": "open", "r_result": None,
            "bars_held": n - start_index, "max_tp_reached": max_tp_reached}


def run_backtest_for_pair(pair, m, start_date, end_date):
    t_times, t_opens, t_highs, t_lows, t_closes = fetch_historical(
        pair, m.TF_TREND, start_date, end_date, m.API_KEY, m.API_CALL_SLEEP)
    time.sleep(m.API_CALL_SLEEP)
    s_times, s_opens, s_highs, s_lows, s_closes = fetch_historical(
        pair, m.TF_STRUCTURE, start_date, end_date, m.API_KEY, m.API_CALL_SLEEP)
    time.sleep(m.API_CALL_SLEEP)
    e_times, e_opens, e_highs, e_lows, e_closes = fetch_historical(
        pair, m.TF_ENTRY, start_date, end_date, m.API_KEY, m.API_CALL_SLEEP)

    if len(e_times) < WARMUP_BARS or len(t_times) < 5 or len(s_times) < 5:
        print(f"[{pair}] Not enough history in range — skipping.")
        return []

    trades = []
    # pair_state mirrors what live keeps in state.json for this pair —
    # in particular "order_blocks", which sync_order_blocks /
    # update_order_block_mitigation / active_zones_for all read and
    # write directly, exactly as they do against the real state dict.
    pair_state = {}
    setup = None
    bias = None
    last_trend_calc_time = None
    last_structure_calc_time = None
    trend_ptr = 0
    structure_ptr = 0

    for i in range(WARMUP_BARS, len(e_times)):
        t = e_times[i]
        t_dt = parse_bar_time(t)

        # ---- trend bias, refreshed on the same cadence as live caching ----
        trend_ptr = advance_closed_index(t_times, t, trend_ptr)
        need_trend_refresh = (
            last_trend_calc_time is None or
            (t_dt - parse_bar_time(last_trend_calc_time)).total_seconds() / 60 >= m.TREND_CACHE_MINUTES
        )
        bias_flipped = False
        if need_trend_refresh and trend_ptr >= 5:
            new_bias = m.get_bias(t_highs[:trend_ptr + 1], t_lows[:trend_ptr + 1])
            last_trend_calc_time = t
            prior_bias = bias
            bias_flipped = prior_bias is not None and new_bias is not None and prior_bias != new_bias
            if bias_flipped:
                setup = None
            bias = new_bias

        if bias is None:
            continue

        # ---- structure break, refreshed on the same cadence as live caching ----
        structure_ptr = advance_closed_index(s_times, t, structure_ptr)
        need_structure_refresh = (
            last_structure_calc_time is None or
            (t_dt - parse_bar_time(last_structure_calc_time)).total_seconds() / 60 >= m.STRUCTURE_CACHE_MINUTES
        )
        if need_structure_refresh and structure_ptr >= 5:
            s_h = s_highs[:structure_ptr + 1]
            s_l = s_lows[:structure_ptr + 1]
            s_c = s_closes[:structure_ptr + 1]
            s_o = s_opens[:structure_ptr + 1]
            atr_s = m.atr(s_h, s_l, s_c, 14)
            bos = m.check_structure_break(
                s_h, s_l, s_c, bias,
                displacement_atr_mult=(m.DISPLACEMENT_ATR_MULT if m.ENTRY_MODE == "structure" else None),
                atr_val=atr_s,
            )
            zones, liquidity_ok, sr_ok = [], True, True
            if bos and m.ENTRY_MODE == "structure":
                bos_level, pullback_zone, bos_index = bos
                zones = m.find_order_blocks(s_o, s_h, s_l, s_c, bias, bos_index, m.OB_LOOKBACK, m.OB_MAX_ZONES)
                if len(zones) < m.OB_MAX_ZONES:
                    sd_zone = m.find_supply_demand_zone(
                        s_o, s_h, s_l, s_c, bias, bos_index, atr_s,
                        m.SD_CONSOLIDATION_BARS, m.SD_MOVE_ATR_MULT,
                    )
                    if sd_zone:
                        zones.append(sd_zone)
                if m.LIQUIDITY_LOOKBACK > 0:
                    tol = m.SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
                    swings_s = m.find_swings(s_h, s_l, m.SWING_LOOKBACK)
                    pools = m.find_liquidity_pools(swings_s, tol)
                    liquidity_ok = m.liquidity_swept_before_break(pools, bias, bos_index, m.LIQUIDITY_LOOKBACK)
                if m.SR_MIN_TOUCHES > 0:
                    if zones:
                        level = zones[0]["low"] if bias == "bullish" else zones[0]["high"]
                        tol = m.SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
                        touches = m.count_level_touches(s_h, s_l, level, tol, len(s_h), bos_index)
                        sr_ok = touches >= m.SR_MIN_TOUCHES
                    else:
                        sr_ok = False

                # Same persistent order-block store the live script keeps
                # in state.json — synced and mitigation-checked here too.
                m.sync_order_blocks(pair_state, bias, zones, bias_flipped, m.OB_MAX_ZONES)
                m.update_order_block_mitigation(pair_state.get("order_blocks", []), s_c[-1])

            last_structure_calc_time = t
            if bos:
                bos_level, pullback_zone, bos_index = bos
                is_new_bos = not setup or setup.get("bos_level") != bos_level
                if is_new_bos:
                    setup = {"bos_level": bos_level, "pullback_zone": pullback_zone, "confirmed": False}
                    if m.ENTRY_MODE == "structure":
                        setup["zones"] = zones
                        setup["liquidity_ok"] = liquidity_ok
                        setup["sr_ok"] = sr_ok
                        setup["is_choch"] = bias_flipped

        if not setup or setup.get("confirmed"):
            continue

        if m.ENTRY_MODE == "structure":
            if not setup.get("zones") or not setup.get("liquidity_ok", True) or not setup.get("sr_ok", True):
                continue

        # ---- 5M entry confirmation ----
        eh, el, ec, eo, et = e_highs[:i + 1], e_lows[:i + 1], e_closes[:i + 1], e_opens[:i + 1], e_times[:i + 1]
        a5 = m.atr(eh, el, ec, 14) or 0

        if m.ENTRY_MODE == "retest":
            confirmation = m.check_retest_confirmation(eh, el, ec, bias, setup["bos_level"], a5)
        elif m.ENTRY_MODE == "structure":
            m.update_order_block_mitigation(pair_state.get("order_blocks", []), ec[-1])
            active_zones = m.active_zones_for(pair_state, bias)
            confirmation = None
            if active_zones:
                confirmation = m.check_smc_confirmation(
                    et, eo, eh, el, ec, bias, active_zones, m.SESSION_START_UTC, m.SESSION_END_UTC)
        elif m.ENTRY_MODE == "retest_or_pullback":
            confirmation = m.check_retest_confirmation(eh, el, ec, bias, setup["bos_level"], a5)
            trigger = "retest"
            if not confirmation:
                confirmation = m.check_entry_confirmation(
                    eh, el, ec, bias, setup["pullback_zone"], setup["bos_level"], m.SWING_ENTRY_MODE)
                trigger = "pullback"
            if confirmation:
                confirmation["trigger"] = trigger
        else:
            confirmation = m.check_entry_confirmation(
                eh, el, ec, bias, setup["pullback_zone"], setup["bos_level"], m.SWING_ENTRY_MODE)

        if not confirmation:
            continue

        condition_label = None
        duration_class = "day"
        if m.ENTRY_MODE == "structure":
            confs = confirmation.get("confirmations", {})
            full_confirmations = {
                "fvg": confs.get("fvg", False),
                "liquidity": setup.get("liquidity_ok", False),
                "displacement": True,
                "rejection": confs.get("rejection", False),
            }
            sr_hit = m.SR_MIN_TOUCHES > 0 and setup.get("sr_ok", False)
            sd_hit = confirmation.get("zone_type") in ("demand_zone", "supply_zone")
            is_choch = setup.get("is_choch", False)
            condition_label = m.build_condition_label(is_choch, full_confirmations, sr_hit, sd_hit)
            duration_class, _ = m.classify_conviction(is_choch, full_confirmations, sr_hit, sd_hit)

        entry = confirmation["entry"]
        buf = m.SL_BUFFER_ATR_MULT * a5
        if bias == "bullish":
            sl = confirmation["sl_anchor"] - buf
            r = entry - sl
            tps = [entry + mult * r for mult in m.TP_MULTIPLES]
        else:
            sl = confirmation["sl_anchor"] + buf
            r = sl - entry
            tps = [entry - mult * r for mult in m.TP_MULTIPLES]

        setup["confirmed"] = True  # setup is done either way — matches live

        if r <= 0:
            continue

        outcome = simulate_trade_outcome(e_highs, e_lows, bias, i + 1, sl, tps)
        trade = {
            "pair": pair,
            "signal": "BUY" if bias == "bullish" else "SELL",
            "time": t,
            "entry": entry,
            "sl": sl,
            "tp1": tps[0],
            "r_size": r,
            "condition_label": condition_label,
            "duration_class": duration_class,
            **outcome,
        }
        trades.append(trade)

    return trades


def summarize(trades):
    closed = [tr for tr in trades if tr["outcome"] in ("win", "loss")]
    wins = [tr for tr in closed if tr["outcome"] == "win"]
    losses = [tr for tr in closed if tr["outcome"] == "loss"]
    open_trades = [tr for tr in trades if tr["outcome"] == "open"]
    total_r = sum(tr["r_result"] for tr in closed)
    win_rate = (len(wins) / len(closed)) if closed else None
    avg_bars_win = (sum(tr["bars_held"] for tr in wins) / len(wins)) if wins else None
    avg_bars_loss = (sum(tr["bars_held"] for tr in losses) / len(losses)) if losses else None
    runners = [tr["max_tp_reached"] for tr in wins]
    avg_runner = (sum(runners) / len(runners)) if runners else None

    stats = {
        "total_signals": len(trades),
        "closed_trades": len(closed),
        "open_trades": len(open_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(win_rate * 100, 1) if win_rate is not None else None,
        "total_r": round(total_r, 2),
        "avg_bars_held_win": round(avg_bars_win, 1) if avg_bars_win else None,
        "avg_bars_held_loss": round(avg_bars_loss, 1) if avg_bars_loss else None,
        "avg_max_tp_level_reached_on_wins": round(avg_runner, 2) if avg_runner else None,
    }

    # Structure mode only: breakdown by conviction-based duration class,
    # since that's a meaningful split the live script itself makes.
    classes = {tr.get("duration_class") for tr in trades if tr.get("duration_class")}
    if classes - {"day"} or (classes and any(tr.get("condition_label") for tr in trades)):
        by_class = {}
        for cls in sorted(classes):
            cls_trades = [tr for tr in trades if tr.get("duration_class") == cls]
            by_class[cls] = summarize_basic(cls_trades)
        stats["by_duration_class"] = by_class

    return stats


def summarize_basic(trades):
    """Same win/loss/R math as summarize(), without the recursive
    duration-class breakdown — used for the per-class sub-summaries."""
    closed = [tr for tr in trades if tr["outcome"] in ("win", "loss")]
    wins = [tr for tr in closed if tr["outcome"] == "win"]
    win_rate = (len(wins) / len(closed)) if closed else None
    total_r = sum(tr["r_result"] for tr in closed)
    return {
        "total_signals": len(trades),
        "closed_trades": len(closed),
        "wins": len(wins),
        "win_rate_pct": round(win_rate * 100, 1) if win_rate is not None else None,
        "total_r": round(total_r, 2),
    }


def main():
    import forex_alert as m  # reuse the live script's exact logic + config

    start_date = os.environ.get("BACKTEST_START_DATE", "").strip()
    end_date = os.environ.get("BACKTEST_END_DATE", "").strip()
    if not start_date or not end_date:
        days = int(os.environ.get("BACKTEST_DAYS", "14"))
        end_dt = datetime.now(timezone.utc)
        start_dt = end_dt - timedelta(days=days)
        start_date = start_date or start_dt.strftime("%Y-%m-%d")
        end_date = end_date or end_dt.strftime("%Y-%m-%d")

    all_trades = []
    per_pair_stats = {}
    for pair in m.PAIRS:
        print(f"=== Backtesting {pair} ({start_date} to {end_date}, ENTRY_MODE={m.ENTRY_MODE}) ===")
        try:
            trades = run_backtest_for_pair(pair, m, start_date, end_date)
        except Exception as e:
            print(f"ERROR backtesting {pair}: {e}", file=sys.stderr)
            continue
        all_trades.extend(trades)
        stats = summarize(trades)
        per_pair_stats[pair] = stats
        print(json.dumps(stats, indent=2))

    overall = summarize(all_trades)
    print("=== OVERALL ===")
    print(json.dumps(overall, indent=2))

    out_path = os.path.join(os.path.dirname(__file__), "backtest_results.json")
    with open(out_path, "w") as f:
        json.dump({
            "config": {
                "entry_mode": m.ENTRY_MODE,
                "pairs": m.PAIRS,
                "tf_trend": m.TF_TREND,
                "tf_structure": m.TF_STRUCTURE,
                "tf_entry": m.TF_ENTRY,
                "start_date": start_date,
                "end_date": end_date,
            },
            "overall": overall,
            "per_pair": per_pair_stats,
            "trades": all_trades,
        }, f, indent=2)
    print(f"Wrote {out_path}")

    if os.environ.get("BACKTEST_SEND_TELEGRAM_SUMMARY", "false").lower() == "true":
        try:
            label = f"[{m.STRATEGY_LABEL}] " if m.STRATEGY_LABEL else ""
            msg = (
                f"📊 {label}Backtest {start_date} → {end_date} ({m.ENTRY_MODE})\n"
                f"Signals: {overall['total_signals']} | Closed: {overall['closed_trades']} | "
                f"Win rate: {overall['win_rate_pct']}% | Total R: {overall['total_r']}"
            )
            m.send_telegram(msg)
        except Exception as e:
            print(f"Telegram summary failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
