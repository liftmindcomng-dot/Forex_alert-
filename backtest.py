"""
Backtest for forex_alert.py's ENTRY_MODE=structure (swing) logic.

Put this file next to forex_alert.py and run:

    python backtest.py --pages 3            # pulls ~150 days of XAU/USD 15min from Twelve Data
    python backtest.py --csv xauusd_15m.csv # or use your own file (datetime,open,high,low,close)

It IMPORTS the real detection functions from forex_alert.py (bias, structure
break, order blocks, breakers, mitigation blocks, engulfing/rejection/FVG
confirmation, session filter, conviction scoring) and replays them bar by
bar, so it tests the rules you actually run, not a re-implementation.

The bot only sends alerts and never manages exits, so outcomes are simulated:
every signal is evaluated as a single-target trade for each of TP1..TP5
(win = +kR, loss = -1R, SL assumed to fill first if SL and TP hit in the
same bar, spread deducted). It sweeps the SL buffer so you can see whether
widening it helps, and runs random "control" trades taken with the 4H bias
so you can see whether the entry logic adds anything beyond just trading
with the trend.

Assumptions worth knowing:
  - 1H and 4H bars are built from the 15min data and only COMPLETED bars are
    used (no look-ahead). Live, Twelve Data's 4H bar alignment can differ.
  - Signals are evaluated independently and can overlap in time.
  - Every result comes with a rough 95% interval; with few trades it will be wide.
"""

import os
import sys
import json
import time
import math
import random
import argparse
import statistics
import urllib.request
import urllib.parse

# ---- mirror the swing workflow's env BEFORE importing forex_alert ----
_DEFAULTS = {
    "TWELVE_DATA_API_KEY": "x", "TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_CHAT_ID": "x",
    "ENTRY_MODE": "structure", "SWING_LOOKBACK": "2", "OB_LOOKBACK": "15",
    "OB_MAX_ZONES": "3", "OB_MIN_MOVE_ATR_MULT": "1.0", "REJECTION_WICK_RATIO": "0.5",
    "LIQUIDITY_LOOKBACK": "20", "DISPLACEMENT_ATR_MULT": "1.0", "SR_MIN_TOUCHES": "0",
    "SR_TOUCH_TOLERANCE_ATR_MULT": "0.25", "SD_CONSOLIDATION_BARS": "3",
    "SD_MOVE_ATR_MULT": "1.5", "SESSION_START_UTC": "7", "SESSION_END_UTC": "21",
    "SESSION2_ENABLED": "true", "SESSION2_START_UTC": "21", "SESSION2_END_UTC": "9",
    "PD_FILTER_ENABLED": "false", "OTE_FILTER_ENABLED": "false",
}
for _k, _v in _DEFAULTS.items():
    os.environ.setdefault(_k, _v)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import pandas as pd
import forex_alert as fa

TPS = (1, 2, 3, 4, 5)


# ---------------- data ----------------

def fetch_history(pair, interval, pages, api_key):
    frames, end = [], None
    for p in range(pages):
        params = {"symbol": pair, "interval": interval, "outputsize": 5000,
                  "apikey": api_key, "timezone": "UTC"}
        if end:
            params["end_date"] = end
        url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = json.loads(resp.read().decode())
        if "values" not in data:
            raise RuntimeError(f"Twelve Data error: {data.get('message', data)}")
        rows = data["values"]  # newest first
        frames.append(pd.DataFrame(rows))
        print(f"  page {p + 1}: {len(rows)} bars, oldest {rows[-1]['datetime']}")
        end = rows[-1]["datetime"]
        if len(rows) < 5000:
            break
        time.sleep(8)  # free-tier rate limit
    df = pd.concat(frames)
    df["datetime"] = pd.to_datetime(df["datetime"])
    return clean(df.set_index("datetime"))


def load_csv(path):
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = "datetime" if "datetime" in df.columns else df.columns[0]
    df[tcol] = pd.to_datetime(df[tcol])
    return clean(df.set_index(tcol))


def clean(df):
    df = df[["open", "high", "low", "close"]].astype(float)
    df = df[~df.index.duplicated()].sort_index()
    return df


def synthetic(n=8000, seed=1):
    """Random-walk 15min bars on weekdays. ONLY for smoke-testing that the
    harness runs. Results on this data mean nothing."""
    rng = random.Random(seed)
    idx = pd.date_range("2026-03-02", periods=n * 2, freq="15min")
    idx = idx[idx.dayofweek < 5][:n]
    price, drift, vol, rows = 4200.0, 0.0, 1.5, []
    for i in range(len(idx)):
        if i % 300 == 0:
            drift = rng.uniform(-0.25, 0.25)
        vol = max(0.6, min(4.0, vol + rng.uniform(-0.15, 0.15)))
        o = price
        c = o + drift + rng.gauss(0, vol)
        h = max(o, c) + abs(rng.gauss(0, vol * 0.6))
        l = min(o, c) - abs(rng.gauss(0, vol * 0.6))
        rows.append((o, h, l, c))
        price = c
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def resample_ohlc(df, rule):
    o = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    o["end"] = o.index + pd.Timedelta(rule)
    return o


# ---------------- replay of process_pair (structure mode) ----------------

def replay(df15, warmup=250):
    n = len(df15)
    idx = df15.index
    times = [t.strftime("%Y-%m-%d %H:%M:%S") for t in idx]
    O, H, L, C = (df15[c].tolist() for c in ("open", "high", "low", "close"))
    d1, d4 = resample_ohlc(df15, "1h"), resample_ohlc(df15, "4h")
    e1, e4 = d1["end"].values, d4["end"].values
    end15 = (idx + pd.Timedelta("15min")).values
    o1, h1, l1, c1 = (d1[c].tolist() for c in ("open", "high", "low", "close"))
    h4, l4 = d4["high"].tolist(), d4["low"].tolist()

    st = {"order_blocks": [], "breaker_blocks": [], "mitigation_blocks": []}
    setup, prior_bias, bias, k4_last = None, None, None, -1
    cache, cache_k1, cache_bias = None, -1, None
    bias_at, signals = [None] * n, []

    for i in range(warmup, n):
        k4 = int(np.searchsorted(e4, end15[i], side="right"))
        k1 = int(np.searchsorted(e1, end15[i], side="right"))
        if k4 < 40 or k1 < 40:
            continue

        if k4 != k4_last:
            s = max(0, k4 - 120)
            bias = fa.get_bias(h4[s:k4], l4[s:k4])
            k4_last = k4
        bias_at[i] = bias
        if bias is None:
            setup = None
            continue

        flipped = prior_bias is not None and prior_bias != bias
        if flipped:
            setup = None
            st["mitigation_blocks"] = []
        prior_bias = bias

        # --- structure refresh (once per completed 1H bar, or on bias change) ---
        if cache is None or k1 != cache_k1 or cache_bias != bias:
            s = max(0, k1 - 150)
            ho, hh, hl, hc = o1[s:k1], h1[s:k1], l1[s:k1], c1[s:k1]
            atr_s = fa.atr(hh, hl, hc, 14)
            bos = fa.check_structure_break(hh, hl, hc, bias,
                                           displacement_atr_mult=fa.DISPLACEMENT_ATR_MULT, atr_val=atr_s)
            zones, liq_ok, sr_ok = [], True, True
            if bos:
                _, _, bos_idx = bos
                swings_s = fa.find_swings(hh, hl, fa.SWING_LOOKBACK)
                tol = fa.SR_TOUCH_TOLERANCE_ATR_MULT * atr_s if atr_s else 0
                pools = fa.find_liquidity_pools(swings_s, tol)
                zones = fa.find_order_blocks(ho, hh, hl, hc, bias, bos_idx, fa.OB_LOOKBACK,
                                             fa.OB_MAX_ZONES, atr_val=atr_s,
                                             min_move_atr_mult=fa.OB_MIN_MOVE_ATR_MULT)
                if len(zones) < fa.OB_MAX_ZONES:
                    sd = fa.find_supply_demand_zone(ho, hh, hl, hc, bias, bos_idx, atr_s,
                                                    fa.SD_CONSOLIDATION_BARS, fa.SD_MOVE_ATR_MULT)
                    if sd:
                        zones.append(sd)
                if fa.LIQUIDITY_LOOKBACK > 0:
                    liq_ok = fa.liquidity_swept_before_break(pools, bias, bos_idx, fa.LIQUIDITY_LOOKBACK)
                if fa.SR_MIN_TOUCHES > 0:
                    if zones:
                        level = zones[0]["low"] if bias == "bullish" else zones[0]["high"]
                        touches = fa.count_level_touches(hh, hl, level, tol, len(hh), bos_idx)
                        sr_ok = touches >= fa.SR_MIN_TOUCHES
                    else:
                        sr_ok = False
                fa.sync_order_blocks(st, bias, zones, flipped, fa.OB_MAX_ZONES)
                fa.update_order_block_mitigation(st["order_blocks"], hc[-1],
                                                 breaker_blocks=st["breaker_blocks"],
                                                 mitigation_blocks=st["mitigation_blocks"])
                fa.update_breaker_mitigation(st["breaker_blocks"], hc[-1])
                fa.prune_order_blocks(st["breaker_blocks"], fa.OB_MAX_ZONES)
                fa.prune_order_blocks(st["mitigation_blocks"], fa.OB_MAX_ZONES)
            cache = {"bos": bos, "zones": zones, "liq_ok": liq_ok, "sr_ok": sr_ok,
                     "slices": (ho, hh, hl, hc), "atr": atr_s}
            cache_k1, cache_bias = k1, bias

        bos = cache["bos"]
        if bos:
            bos_level, pz, _ = bos
            if setup is None or setup["bos_level"] != bos_level:
                setup = {"bos_level": bos_level, "pullback_zone": pz, "confirmed": False,
                         "is_choch": flipped, "zones": cache["zones"],
                         "liq_ok": cache["liq_ok"], "sr_ok": cache["sr_ok"]}
        if setup is None or setup["confirmed"]:
            continue
        if not setup["zones"] and not st["breaker_blocks"] and not st["mitigation_blocks"]:
            continue
        if not setup["sr_ok"]:
            continue

        # --- entry check on the 15min bar ---
        s = max(0, i - 149)
        t5, o5, h5, l5, c5 = times[s:i + 1], O[s:i + 1], H[s:i + 1], L[s:i + 1], C[s:i + 1]
        a5 = fa.atr(h5, l5, c5, 14) or 0
        fa.update_order_block_mitigation(st["order_blocks"], c5[-1],
                                         breaker_blocks=st["breaker_blocks"],
                                         mitigation_blocks=st["mitigation_blocks"])
        fa.update_breaker_mitigation(st["breaker_blocks"], c5[-1])

        def zones_now():
            return (fa.active_zones_for(st, bias) + fa.active_breaker_zones_for(st, bias)
                    + fa.active_mitigation_zones_for(st, bias))

        active = zones_now()
        if not active:
            ho, hh, hl, hc = cache["slices"]
            atr_s = cache["atr"]
            bos2 = fa.check_structure_break(hh, hl, hc, bias,
                                            displacement_atr_mult=fa.DISPLACEMENT_ATR_MULT, atr_val=atr_s)
            if bos2:
                fresh = fa.find_order_blocks(ho, hh, hl, hc, bias, bos2[2], fa.OB_LOOKBACK,
                                             fa.OB_MAX_ZONES, atr_val=atr_s,
                                             min_move_atr_mult=fa.OB_MIN_MOVE_ATR_MULT)
                if len(fresh) < fa.OB_MAX_ZONES:
                    sd = fa.find_supply_demand_zone(ho, hh, hl, hc, bias, bos2[2], atr_s,
                                                    fa.SD_CONSOLIDATION_BARS, fa.SD_MOVE_ATR_MULT)
                    if sd:
                        fresh.append(sd)
                if fresh:
                    fa.sync_order_blocks(st, bias, fresh, False, fa.OB_MAX_ZONES)
                    fa.update_order_block_mitigation(st["order_blocks"], c5[-1],
                                                     breaker_blocks=st["breaker_blocks"],
                                                     mitigation_blocks=st["mitigation_blocks"])
                    fa.update_breaker_mitigation(st["breaker_blocks"], c5[-1])
                    fa.prune_order_blocks(st["breaker_blocks"], fa.OB_MAX_ZONES)
                    fa.prune_order_blocks(st["mitigation_blocks"], fa.OB_MAX_ZONES)
                    active = zones_now()
        if not active:
            continue

        conf = fa.check_smc_confirmation(t5, o5, h5, l5, c5, bias, active,
                                         fa.SESSION_START_UTC, fa.SESSION_END_UTC,
                                         bos_level=setup["bos_level"],
                                         pullback_zone=setup["pullback_zone"])
        if not conf:
            continue

        cf = conf["confirmations"]
        full = {"fvg": cf.get("fvg", False), "liquidity": setup["liq_ok"],
                "displacement": True, "rejection": cf.get("rejection", False)}
        zt = conf["zone_type"]
        sr_hit = fa.SR_MIN_TOUCHES > 0 and setup["sr_ok"]
        dur, _ = fa.classify_conviction(setup["is_choch"], full, sr_hit,
                                        zt in ("demand_zone", "supply_zone"),
                                        zt == "breaker_block", zt == "mitigation_block",
                                        cf.get("ote", False))
        signals.append({"i": i, "time": times[i], "side": "BUY" if bias == "bullish" else "SELL",
                        "entry": conf["entry"], "anchor": conf["sl_anchor"], "a5": a5,
                        "zone": zt, "conviction": dur, "choch": setup["is_choch"]})
        setup["confirmed"] = True

    return signals, bias_at, (times, H, L, C)


# ---------------- trade simulation ----------------

def simulate(side, entry, anchor, a5, i, buf, spread, max_hold, H, L, C):
    """Returns {k: R} for each TP multiple, or None if the stop is invalid."""
    if side == "BUY":
        sl = anchor - buf * a5
        r = entry - sl
    else:
        sl = anchor + buf * a5
        r = sl - entry
    if r <= 0:
        return None
    sl_bar, tp_bar = None, {k: None for k in TPS}
    last = min(len(C) - 1, i + max_hold)
    for j in range(i + 1, last + 1):
        if side == "BUY":
            if L[j] <= sl:
                sl_bar = j
                break
            for k in TPS:
                if tp_bar[k] is None and H[j] >= entry + k * r:
                    tp_bar[k] = j
        else:
            if H[j] >= sl:
                sl_bar = j
                break
            for k in TPS:
                if tp_bar[k] is None and L[j] <= entry - k * r:
                    tp_bar[k] = j
        if all(tp_bar[k] is not None for k in TPS):
            break
    mtm = ((C[last] - entry) if side == "BUY" else (entry - C[last])) / r
    cost = spread / r
    out = {}
    for k in TPS:
        if tp_bar[k] is not None:
            out[k] = k - cost
        elif sl_bar is not None:
            out[k] = -1 - cost
        else:
            out[k] = mtm - cost
    return out, r


def summarize(rs):
    n = len(rs)
    if n == 0:
        return None
    wins = sum(1 for x in rs if x > 0)
    gp = sum(x for x in rs if x > 0)
    gl = -sum(x for x in rs if x < 0)
    eq = peak = dd = 0.0
    for x in rs:
        eq += x
        peak = max(peak, eq)
        dd = max(dd, peak - eq)
    avg = sum(rs) / n
    ci = 1.96 * statistics.stdev(rs) / math.sqrt(n) if n > 1 else float("nan")
    return {"n": n, "win": 100 * wins / n, "avg": avg, "ci": ci,
            "pf": (gp / gl) if gl > 0 else float("inf"), "dd": dd, "tot": sum(rs)}


def row(label, s):
    if not s:
        return f"  {label:<8} no trades"
    return (f"  {label:<8} n={s['n']:<4} win={s['win']:5.1f}%  avgR={s['avg']:+.2f} (±{s['ci']:.2f})  "
            f"PF={s['pf']:.2f}  maxDD={s['dd']:.1f}R  totR={s['tot']:+.1f}")


def controls(bias_at, arrays, per_signal, ratio, buf_spread, max_hold, warmup, rng):
    times, H, L, C = arrays
    n = len(C)
    pool = [j for j in range(warmup, n - max_hold - 1)
            if bias_at[j] is not None and fa.in_any_session(times[j])]
    out = {k: [] for k in TPS}
    for _ in range(per_signal):
        if not pool:
            break
        j = rng.choice(pool)
        s = max(0, j - 149)
        a = fa.atr(H[s:j + 1], L[s:j + 1], C[s:j + 1], 14)
        if not a:
            continue
        side = "BUY" if bias_at[j] == "bullish" else "SELL"
        entry = C[j]
        anchor = entry - ratio * a if side == "BUY" else entry + ratio * a
        res = simulate(side, entry, anchor, a, j, 0.0, buf_spread, max_hold, H, L, C)
        if res:
            for k in TPS:
                out[k].append(res[0][k])
    return out


# ---------------- main ----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--pair", default="XAU/USD")
    ap.add_argument("--pages", type=int, default=3)
    ap.add_argument("--buffers", default="0.15,0.3,0.5,0.75,1.0")
    ap.add_argument("--spread", type=float, default=0.30, help="round-trip cost in price units ($)")
    ap.add_argument("--max-hold-bars", type=int, default=288, help="15min bars (288 = 72h)")
    ap.add_argument("--controls", type=int, default=20, help="random control trades per signal")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--synthetic", action="store_true", help="smoke test on fake data")
    a = ap.parse_args()

    if a.synthetic:
        df = synthetic()
        print("*** SMOKE TEST ON SYNTHETIC DATA — numbers below are meaningless ***\n")
    elif a.csv:
        df = load_csv(a.csv)
    else:
        key = os.environ.get("TWELVE_DATA_API_KEY", "")
        if key in ("", "x"):
            sys.exit("Set TWELVE_DATA_API_KEY in your environment, or pass --csv.")
        print("Fetching history...")
        df = fetch_history(a.pair, "15min", a.pages, key)

    print(f"Data: {len(df)} x 15min bars, {df.index[0]} -> {df.index[-1]}")
    warmup = 250
    signals, bias_at, arrays = replay(df, warmup)
    times, H, L, C = arrays
    nb = sum(1 for s in signals if s["side"] == "BUY")
    print(f"Signals: {len(signals)} ({nb} buys / {len(signals) - nb} sells)")
    if len(signals) < 30:
        print("WARNING: fewer than 30 signals — too few to conclude anything. Use more history.")
    if not signals:
        return

    rng = random.Random(a.seed)
    buffers = [float(x) for x in a.buffers.split(",")]
    ref_buf = buffers[0]

    for buf in buffers:
        sims = []
        for s in signals:
            r = simulate(s["side"], s["entry"], s["anchor"], s["a5"], s["i"], buf,
                         a.spread, a.max_hold_bars, H, L, C)
            if r:
                sims.append((s, r[0], r[1] / s["a5"] if s["a5"] else 0))
        if not sims:
            continue
        ratio = statistics.median(x[2] for x in sims)
        ctrl = controls(bias_at, arrays, a.controls * len(sims), ratio, a.spread,
                        a.max_hold_bars, warmup, rng)
        print(f"\n=== SL buffer {buf} x ATR  (median risk = {ratio:.2f} x ATR15) ===")
        for k in TPS:
            print(row(f"TP{k}", summarize([x[1][k] for x in sims])))
            print(row(f"  ctrl", summarize(ctrl[k])) + "   <- random trades with the 4H bias")

    print(f"\n=== Breakdown at SL buffer {ref_buf}, TP2 ===")
    for field in ("zone", "conviction", "side"):
        print(f" by {field}:")
        for v in sorted({s[field] for s in signals}):
            rs = []
            for s in signals:
                if s[field] != v:
                    continue
                r = simulate(s["side"], s["entry"], s["anchor"], s["a5"], s["i"], ref_buf,
                             a.spread, a.max_hold_bars, H, L, C)
                if r:
                    rs.append(r[0][2])
            print(row(str(v), summarize(rs)))

    print("\nRead it like this: avgR is the average result per trade in units of risk.")
    print("If avgR's ± interval spans zero, the data can't tell you there's an edge.")
    print("If the 'ctrl' rows look as good as the signal rows, the entry logic adds")
    print("nothing beyond trading with the 4H trend. Compare across DIFFERENT market")
    print("periods before trusting any of it.")


if __name__ == "__main__":
    main()
