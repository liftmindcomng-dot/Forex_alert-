"""
Multi-strategy backtest for forex_alert.py.

Put next to forex_alert.py and run:

    python backtest.py --pages 8               # ~5 months of XAU/USD 5min from Twelve Data
    python backtest.py --csv xauusd_5m.csv     # or your own 5-MINUTE file (datetime,open,high,low,close)
    python backtest.py --only smc_swing,intraday   # run just some strategies

It IMPORTS the real detection functions from forex_alert.py and replays them
bar by bar on 5min data (15min / 1h / 4h bars are built from it, using only
COMPLETED bars, so no look-ahead). Strategies tested:

  intraday       retest_or_pullback   15m structure / 5m entry
  retest         retest only          15m / 5m
  pullback       plain pullback       15m / 5m
  pullback_deep  50-79% retrace       15m / 5m
  smc_swing      structure (SMC)      1h  / 15m   <- your swing workflow
  smc_swing_pd   ... + premium/discount filter
  smc_swing_ote  ... + OTE band filter
  smc_intraday   structure (SMC)      15m / 5m

The bot only sends alerts and never manages exits, so outcomes are simulated:
each signal is scored as a single-target trade at TP1..TP5 (win = +kR,
loss = -1R, SL assumed first if SL and TP hit in the same bar, spread
deducted). For every strategy it also runs random "control" trades taken with
the 4H bias, so you can see whether the entry logic adds anything beyond just
trading with the trend.

Caveats:
  - Signals are scored independently and can overlap in time.
  - Every average carries a rough 95% interval; overlap makes the true
    uncertainty wider than shown.
  - Many strategies x buffers x targets are tested, so one of them can look
    good by luck. The verdict uses TP2 only, decided in advance, and needs
    both a positive interval AND beating the control.
"""

import os
import sys
import json
import time
import math
import random
import argparse
import statistics
import html
import urllib.request
import urllib.parse

# ---- mirror the workflows' env BEFORE importing forex_alert ----
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

STRATEGIES = [
    {"key": "intraday", "name": "INTRADAY retest_or_pullback (15m struct / 5m entry)",
     "mode": "retest_or_pullback", "struct": 15, "entry": 5},
    {"key": "retest", "name": "RETEST only (15m / 5m)",
     "mode": "retest", "struct": 15, "entry": 5},
    {"key": "pullback", "name": "PULLBACK plain (15m / 5m)",
     "mode": "pullback", "struct": 15, "entry": 5, "deep": False},
    {"key": "pullback_deep", "name": "PULLBACK deep 50-79% (15m / 5m)",
     "mode": "pullback", "struct": 15, "entry": 5, "deep": True},
    {"key": "smc_swing", "name": "SMC SWING structure (1h struct / 15m entry)",
     "mode": "structure", "struct": 60, "entry": 15},
    {"key": "smc_swing_pd", "name": "SMC SWING + premium/discount filter",
     "mode": "structure", "struct": 60, "entry": 15, "pd": True},
    {"key": "smc_swing_ote", "name": "SMC SWING + OTE band filter",
     "mode": "structure", "struct": 60, "entry": 15, "ote": True},
    {"key": "smc_intraday", "name": "SMC INTRADAY structure (15m struct / 5m entry)",
     "mode": "structure", "struct": 15, "entry": 5},
]


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
        print(f"  page {p + 1}: {len(rows)} bars, oldest {rows[-1]['datetime']}", flush=True)
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
    return df[~df.index.duplicated()].sort_index()


def synthetic(n=30000, seed=1):
    """Random-walk 5min bars on weekdays. ONLY for smoke-testing that the
    harness runs. Results on this data mean nothing."""
    rng = random.Random(seed)
    idx = pd.date_range("2026-03-02", periods=n * 2, freq="5min")
    idx = idx[idx.dayofweek < 5][:n]
    price, drift, vol, rows = 4200.0, 0.0, 0.9, []
    for i in range(len(idx)):
        if i % 900 == 0:
            drift = rng.uniform(-0.1, 0.1)
        vol = max(0.35, min(2.4, vol + rng.uniform(-0.08, 0.08)))
        o = price
        c = o + drift + rng.gauss(0, vol)
        h = max(o, c) + abs(rng.gauss(0, vol * 0.6))
        l = min(o, c) - abs(rng.gauss(0, vol * 0.6))
        rows.append((o, h, l, c))
        price = c
    return pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"])


def resample_ohlc(df, minutes):
    rule = f"{minutes}min"
    o = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    o["end"] = o.index + pd.Timedelta(rule)
    return o


# ---------------- replay of process_pair for any ENTRY_MODE ----------------

def replay(base, cfg, warm_bars=300):
    fa.PD_FILTER_ENABLED = cfg.get("pd", False)
    fa.OTE_FILTER_ENABLED = cfg.get("ote", False)
    mode = cfg["mode"]
    deep = cfg.get("deep", False)
    n = len(base)
    bH, bL, bC = (base[c].tolist() for c in ("high", "low", "close"))
    bend = (base.index + pd.Timedelta("5min")).values

    E = resample_ohlc(base, cfg["entry"])
    S = resample_ohlc(base, cfg["struct"])
    T = resample_ohlc(base, 240)
    ke_arr = np.searchsorted(E["end"].values, bend, side="right")
    ks_arr = np.searchsorted(S["end"].values, bend, side="right")
    kt_arr = np.searchsorted(T["end"].values, bend, side="right")

    e_times = [t.strftime("%Y-%m-%d %H:%M:%S") for t in E.index]
    eo, eh, el, ec = (E[c].tolist() for c in ("open", "high", "low", "close"))
    so, sh, sl_, sc = (S[c].tolist() for c in ("open", "high", "low", "close"))
    th, tl = T["high"].tolist(), T["low"].tolist()

    st = {"order_blocks": [], "breaker_blocks": [], "mitigation_blocks": []}
    setup, prior_bias, bias, kt_last, ke_prev = None, None, None, -1, -1
    cache, cache_ks, cache_bias = None, -1, None
    bias_at, signals = [None] * n, []

    for i in range(warm_bars, n):
        kt, ks, ke = int(kt_arr[i]), int(ks_arr[i]), int(ke_arr[i])
        if kt < 40 or ks < 40 or ke < 20:
            continue
        if kt != kt_last:
            s = max(0, kt - 120)
            bias = fa.get_bias(th[s:kt], tl[s:kt])
            kt_last = kt
        bias_at[i] = bias
        if ke == ke_prev:
            continue  # no new entry-timeframe bar completed on this 5min step
        ke_prev = ke

        if bias is None:
            setup = None
            continue
        flipped = prior_bias is not None and prior_bias != bias
        if flipped:
            setup = None
            st["mitigation_blocks"] = []
        prior_bias = bias

        # --- structure refresh (once per completed structure bar, or on bias change) ---
        if cache is None or ks != cache_ks or cache_bias != bias:
            s = max(0, ks - 150)
            ho, hh, hl, hc = so[s:ks], sh[s:ks], sl_[s:ks], sc[s:ks]
            atr_s = fa.atr(hh, hl, hc, 14)
            bos = fa.check_structure_break(
                hh, hl, hc, bias,
                displacement_atr_mult=(fa.DISPLACEMENT_ATR_MULT if mode == "structure" else None),
                atr_val=atr_s)
            displacement_hit = (bool(atr_s) and (hh[-1] - hl[-1]) >= fa.DISPLACEMENT_ATR_MULT * atr_s) if bos else False
            zones, liq_ok, sr_ok = [], True, True
            if bos and mode == "structure":
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
                        sr_ok = fa.count_level_touches(hh, hl, level, tol, len(hh), bos_idx) >= fa.SR_MIN_TOUCHES
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
                     "disp": displacement_hit, "slices": (ho, hh, hl, hc), "atr": atr_s}
            cache_ks, cache_bias = ks, bias

        bos = cache["bos"]
        if bos:
            bos_level, pz, _ = bos
            if setup is None or setup["bos_level"] != bos_level:
                setup = {"bos_level": bos_level, "pullback_zone": pz, "confirmed": False,
                         "is_choch": flipped, "disp": cache["disp"], "zones": cache["zones"],
                         "liq_ok": cache["liq_ok"], "sr_ok": cache["sr_ok"]}
        if setup is None or setup["confirmed"]:
            continue

        if mode == "structure":
            if not setup["zones"] and not st["breaker_blocks"] and not st["mitigation_blocks"]:
                continue
            if not setup["sr_ok"]:
                continue

        # --- entry check on the entry-timeframe bar ---
        s = max(0, ke - 150)
        t5, o5, h5, l5, c5 = e_times[s:ke], eo[s:ke], eh[s:ke], el[s:ke], ec[s:ke]
        a5 = fa.atr(h5, l5, c5, 14) or 0
        conf, trigger = None, mode

        if mode == "retest":
            conf = fa.check_retest_confirmation(h5, l5, c5, bias, setup["bos_level"], a5)
        elif mode == "pullback":
            conf = fa.check_entry_confirmation(h5, l5, c5, bias, setup["pullback_zone"],
                                               setup["bos_level"], deep)
            trigger = "pullback"
        elif mode == "retest_or_pullback":
            conf = fa.check_retest_confirmation(h5, l5, c5, bias, setup["bos_level"], a5)
            trigger = "retest"
            if not conf:
                conf = fa.check_entry_confirmation(h5, l5, c5, bias, setup["pullback_zone"],
                                                   setup["bos_level"], deep)
                trigger = "pullback"
        else:  # structure
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
                                                displacement_atr_mult=fa.DISPLACEMENT_ATR_MULT,
                                                atr_val=atr_s)
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
            if active:
                conf = fa.check_smc_confirmation(t5, o5, h5, l5, c5, bias, active,
                                                 fa.SESSION_START_UTC, fa.SESSION_END_UTC,
                                                 bos_level=setup["bos_level"],
                                                 pullback_zone=setup["pullback_zone"])
        if not conf:
            continue

        if mode == "structure":
            cf = conf["confirmations"]
            full = {"fvg": cf.get("fvg", False), "liquidity": setup["liq_ok"],
                    "displacement": True, "rejection": cf.get("rejection", False)}
            zt = conf["zone_type"]
            sr_hit = fa.SR_MIN_TOUCHES > 0 and setup["sr_ok"]
            dur, _ = fa.classify_conviction(setup["is_choch"], full, sr_hit,
                                            zt in ("demand_zone", "supply_zone"),
                                            zt == "breaker_block", zt == "mitigation_block",
                                            cf.get("ote", False))
            label = zt
        else:
            dur, _ = fa.classify_conviction_generic(setup["is_choch"], setup["disp"], trigger,
                                                    deep and trigger == "pullback")
            label = trigger
        signals.append({"i": i, "side": "BUY" if bias == "bullish" else "SELL",
                        "entry": conf["entry"], "anchor": conf["sl_anchor"], "a5": a5,
                        "zone": label, "conviction": dur, "choch": setup["is_choch"]})
        setup["confirmed"] = True

    ctx = {"n": n, "H": bH, "L": bL, "C": bC, "bias_at": bias_at, "ke_arr": ke_arr,
           "e_times": e_times, "eh": eh, "el": el, "ec": ec, "mode": mode, "warm": warm_bars}
    return signals, ctx


# ---------------- trade simulation ----------------

def simulate(side, entry, anchor, a5, i, buf, spread, max_hold, H, L, C):
    """Returns ({k: R}, risk_distance) per TP multiple, or None if the stop is invalid."""
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


def controls(ctx, count, ratio, spread, max_hold, rng):
    """Random trades taken WITH the 4H bias at bars where an entry bar just
    completed (session-filtered for structure strategies), sized with the
    same risk-in-ATR as the signals."""
    if "pool" not in ctx:
        ke, bias_at, n = ctx["ke_arr"], ctx["bias_at"], ctx["n"]
        pool = []
        for j in range(ctx["warm"], n - max_hold - 1):
            if bias_at[j] is None or ke[j] == ke[j - 1] or ke[j] < 20:
                continue
            if ctx["mode"] == "structure" and not fa.in_any_session(ctx["e_times"][ke[j] - 1]):
                continue
            pool.append(j)
        ctx["pool"] = pool
    pool = ctx["pool"]
    out = {k: [] for k in TPS}
    if not pool:
        return out
    for _ in range(count):
        j = rng.choice(pool)
        k = int(ctx["ke_arr"][j])
        s = max(0, k - 150)
        a = fa.atr(ctx["eh"][s:k], ctx["el"][s:k], ctx["ec"][s:k], 14)
        if not a:
            continue
        side = "BUY" if ctx["bias_at"][j] == "bullish" else "SELL"
        entry = ctx["C"][j]
        anchor = entry - ratio * a if side == "BUY" else entry + ratio * a
        res = simulate(side, entry, anchor, a, j, 0.0, spread, max_hold, ctx["H"], ctx["L"], ctx["C"])
        if res:
            for t in TPS:
                out[t].append(res[0][t])
    return out


def send_telegram(text):
    """Send `text` to Telegram as monospace <pre> blocks (HTML mode with
    everything escaped, so stray _ * ` characters can't cause a 400 like the
    Markdown mode did earlier). Splits on line boundaries to stay under
    Telegram's 4096-character limit. Never raises."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if token in ("", "x") or chat_id in ("", "x"):
        print("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID) — skipped.")
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):
        # measure AFTER escaping (> becomes &gt; etc.) so the limit holds
        if len(html.escape(cur)) + len(html.escape(line)) + 1 > 3500:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    try:
        for c in chunks:
            payload = urllib.parse.urlencode({
                "chat_id": chat_id,
                "text": "<pre>" + html.escape(c.rstrip("\n")) + "</pre>",
                "parse_mode": "HTML",
            }).encode()
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=payload)
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            time.sleep(1)
        print(f"Sent results to Telegram ({len(chunks)} message(s)).")
        return True
    except Exception as e:
        print(f"Telegram send failed: {e}")
        return False


# ---------------- reporting ----------------

def summarize(rs):
    n = len(rs)
    if n == 0:
        return None
    wins = sum(1 for x in rs if x > 0)
    avg = sum(rs) / n
    ci = 1.96 * statistics.stdev(rs) / math.sqrt(n) if n > 1 else float("nan")
    return {"n": n, "win": 100 * wins / n, "avg": avg, "ci": ci}


def fmt(s):
    if not s:
        return "   n/a    "
    ci = f"{s['ci']:.2f}" if s["ci"] == s["ci"] else "n/a"
    return f"{s['avg']:+.2f}±{ci}"


def verdict(sig, ctl):
    if not sig or sig["n"] < 30:
        return "too few trades"
    if sig["avg"] + sig["ci"] < 0:
        return "NEGATIVE"
    if sig["avg"] - sig["ci"] > 0:
        if ctl and sig["avg"] - ctl["avg"] - math.sqrt(sig["ci"] ** 2 + ctl["ci"] ** 2) > 0:
            return "POSITIVE and beats control"
        return "positive, not clearly better than control"
    return "unclear (interval spans zero)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="5-minute bars: datetime,open,high,low,close")
    ap.add_argument("--pair", default="XAU/USD")
    ap.add_argument("--pages", type=int, default=8, help="5000 x 5min bars per page")
    ap.add_argument("--only", default="", help="comma list of strategy keys")
    ap.add_argument("--buffers", default="0.15,1.0")
    ap.add_argument("--spread", type=float, default=0.30, help="round-trip cost in price units ($)")
    ap.add_argument("--max-hold-hours", type=float, default=72)
    ap.add_argument("--controls", type=int, default=10, help="random control trades per signal")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--synthetic", action="store_true", help="smoke test on fake data")
    ap.add_argument("--no-telegram", action="store_true", help="do not send results to Telegram")
    a = ap.parse_args()

    if a.synthetic:
        base = synthetic()
        print("*** SMOKE TEST ON SYNTHETIC DATA — numbers below are meaningless ***\n")
    elif a.csv:
        base = load_csv(a.csv)
    else:
        key = os.environ.get("TWELVE_DATA_API_KEY", "")
        if key in ("", "x"):
            sys.exit("Set TWELVE_DATA_API_KEY in your environment, or pass --csv.")
        print("Fetching 5min history...", flush=True)
        base = fetch_history(a.pair, "5min", a.pages, key)

    step = base.index.to_series().diff().median()
    if step != pd.Timedelta("5min"):
        sys.exit(f"Data must be 5-minute bars (found median step {step}).")

    print(f"Data: {len(base)} x 5min bars, {base.index[0]} -> {base.index[-1]}")
    max_hold = int(a.max_hold_hours * 12)
    buffers = [float(x) for x in a.buffers.split(",")]
    wanted = {x.strip() for x in a.only.split(",") if x.strip()}
    strategies = [s for s in STRATEGIES if not wanted or s["key"] in wanted]
    rng = random.Random(a.seed)
    mid_idx = len(base) // 2
    summary = []

    for cfg in strategies:
        print(f"\n{'=' * 78}\n{cfg['name']}", flush=True)
        signals, ctx = replay(base, cfg)
        nb = sum(1 for s in signals if s["side"] == "BUY")
        print(f"signals: {len(signals)} ({nb} buys / {len(signals) - nb} sells)")
        if not signals:
            summary.append((cfg["key"], cfg["name"], 0, {}, "no signals"))
            continue
        if len(signals) < 30:
            print("  WARNING: fewer than 30 signals — too few to conclude anything.")

        tp2_by_buf, ref_verdict = {}, ""
        for bi, buf in enumerate(buffers):
            sims = []
            for s in signals:
                r = simulate(s["side"], s["entry"], s["anchor"], s["a5"], s["i"], buf,
                             a.spread, max_hold, ctx["H"], ctx["L"], ctx["C"])
                if r:
                    sims.append((s, r[0], r[1] / s["a5"] if s["a5"] else 0))
            if not sims:
                continue
            ratio = statistics.median(x[2] for x in sims)
            ctrl = controls(ctx, a.controls * len(sims), ratio, a.spread, max_hold, rng)
            print(f"\n  SL buffer {buf} x ATR (median risk {ratio:.2f} x ATR)   avgR ± 95% interval")
            print("    target    " + "".join(f"TP{k:<10}" for k in TPS))
            print("    signals   " + "".join(f"{fmt(summarize([x[1][k] for x in sims])):<12}" for k in TPS))
            print("    control   " + "".join(f"{fmt(summarize(ctrl[k])):<12}" for k in TPS))
            sig2 = summarize([x[1][2] for x in sims])
            ctl2 = summarize(ctrl[2])
            v = verdict(sig2, ctl2)
            tp2_by_buf[buf] = sig2
            print(f"    verdict (TP2): {v}")
            if bi == 0:
                ref_verdict = v
                h1 = [x[1][2] for x in sims if x[0]["i"] < mid_idx]
                h2 = [x[1][2] for x in sims if x[0]["i"] >= mid_idx]
                print(f"    first half of data (n={len(h1)}): {fmt(summarize(h1))}   "
                      f"second half (n={len(h2)}): {fmt(summarize(h2))}")
                for field in ("side", "conviction", "zone"):
                    parts = []
                    for val in sorted({x[0][field] for x in sims}):
                        rs = [x[1][2] for x in sims if x[0][field] == val]
                        sm = summarize(rs)
                        parts.append(f"{val}: {fmt(sm)} (n={sm['n']})")
                    print(f"    by {field}: " + " | ".join(parts))
        summary.append((cfg["key"], cfg["name"], len(signals), tp2_by_buf, ref_verdict))

    print(f"\n{'=' * 78}\nSUMMARY  (TP2 avgR in units of risk; verdict at the first buffer)\n")
    head = f"{'strategy':<52}{'n':>5}  " + "".join(f"buf{b:<8g}" for b in buffers) + " verdict"
    print(head)
    for key, name, n, by_buf, v in summary:
        cols = "".join(f"{fmt(by_buf.get(b)):<12}" for b in buffers)
        print(f"{name[:51]:<52}{n:>5}  {cols} {v}")

    print("\nHow to read this:")
    print("- avgR is the average result per trade in units of risk; ± is a rough 95% interval.")
    print("- 'control' = random trades with the 4H bias. If it matches the signal rows, the")
    print("  entry logic adds nothing beyond trading with the trend.")
    print("- Only 'POSITIVE and beats control' is even mildly encouraging, and with this many")
    print("  strategies tested one can appear by luck. Re-test on other periods/instruments")
    print("  before trusting it, and check first-half vs second-half agree.")

    if not a.no_telegram:
        tg = [f"BACKTEST {a.pair} 5min",
              f"{base.index[0]:%Y-%m-%d} -> {base.index[-1]:%Y-%m-%d}",
              f"spread ${a.spread:g} | max hold {a.max_hold_hours:g}h",
              "TP2 avgR (risk units) +/- 95% interval", ""]
        for key, name, n, by_buf, v in summary:
            tg.append(f"{key} (n={n})")
            if by_buf:
                tg.append("  " + " | ".join(f"buf{b:g}: {fmt(by_buf.get(b))}" for b in buffers))
            tg.append(f"  -> {v}")
            tg.append("")
        tg.append("'POSITIVE' needs a positive interval AND beating")
        tg.append("random 4H-bias trades. Small samples and many")
        tg.append("strategies tested: treat as evidence, not proof.")
        send_telegram("\n".join(tg))


if __name__ == "__main__":
    main()
