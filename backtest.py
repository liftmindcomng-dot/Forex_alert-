#!/usr/bin/env python3
"""
Backtest for the Swing STRUCTURE (SMC) mode of forex_alert.py

Usage:
    python backtest.py                          # default: XAU/USD 2024 → now
    python backtest.py --pair XAU/USD --start 2024-01-01 --end 2026-09-01
    python backtest.py --data path/to/1m.csv    # use local 1-minute CSV

Outputs:
    - backtest_trades.csv
    - backtest_equity.png
    - summary printed to stdout (and written to backtest_summary.txt)
"""

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Parameters – mirror the Swing workflow (check-signal.yml / forex_alert.py)
# ---------------------------------------------------------------------------
SWING_LOOKBACK = 2
SL_BUFFER_ATR_MULT = 0.15
TP_MULTIPLES = (1, 2, 3, 4, 5)
OB_LOOKBACK = 15
OB_MAX_ZONES = 3
REJECTION_WICK_RATIO = 0.6          # required for entry
LIQUIDITY_LOOKBACK = 20
DISPLACEMENT_ATR_MULT = 1.0
SR_MIN_TOUCHES = 2
SR_TOUCH_TOLERANCE_ATR_MULT = 0.25
SD_CONSOLIDATION_BARS = 3
SD_MOVE_ATR_MULT = 1.5
SESSION_START_UTC = 9               # skip early London
SESSION_END_UTC = 21
ATR_PERIOD = 14

# Extra filters (from optimisation)
REQUIRE_REJECTION = True
CHOCH_PREFERRED = True              # pure BOS needs extra confluence


# ---------------------------------------------------------------------------
# Core helpers (ported from forex_alert.py)
# ---------------------------------------------------------------------------

def atr(highs, lows, closes, period=14):
    n = len(closes)
    if n <= period:
        return np.full(n, np.nan)
    tr = np.maximum(
        highs[1:] - lows[1:],
        np.maximum(np.abs(highs[1:] - closes[:-1]), np.abs(lows[1:] - closes[:-1])),
    )
    out = np.empty(n)
    out[:period] = np.nan
    out[period] = tr[:period].mean()
    for i in range(period + 1, n):
        out[i] = (out[i - 1] * (period - 1) + tr[i - 1]) / period
    return out


def find_swings(highs, lows, lookback=2):
    swings = []
    n = len(highs)
    for i in range(lookback, n - lookback):
        if highs[i] == max(highs[i - lookback : i + lookback + 1]) and list(
            highs[i - lookback : i + lookback + 1]
        ).count(highs[i]) == 1:
            swings.append({"i": i, "kind": "high", "price": highs[i]})
        if lows[i] == min(lows[i - lookback : i + lookback + 1]) and list(
            lows[i - lookback : i + lookback + 1]
        ).count(lows[i]) == 1:
            swings.append({"i": i, "kind": "low", "price": lows[i]})
    return swings


def last_two(swings, kind):
    m = [s for s in swings if s["kind"] == kind]
    return m[-2:] if len(m) >= 2 else None


def last_swing_before(swings, kind, before_index):
    m = [s for s in swings if s["kind"] == kind and s["i"] < before_index]
    return m[-1] if m else None


def get_bias(highs, lows, lookback=2):
    swings = find_swings(highs, lows, lookback)
    hh, ll = last_two(swings, "high"), last_two(swings, "low")
    if not hh or not ll:
        return None
    if hh[-1]["price"] > hh[-2]["price"] and ll[-1]["price"] > ll[-2]["price"]:
        return "bullish"
    if hh[-1]["price"] < hh[-2]["price"] and ll[-1]["price"] < ll[-2]["price"]:
        return "bearish"
    return None


def check_structure_break(highs, lows, closes, bias, atr_val, displacement_mult=1.0):
    swings = find_swings(highs, lows, SWING_LOOKBACK)
    n = len(closes)
    last_close = closes[n - 1]

    def displacement_ok():
        if atr_val is None or np.isnan(atr_val):
            return True
        return (highs[n - 1] - lows[n - 1]) >= displacement_mult * atr_val

    if bias == "bullish":
        level = last_swing_before(swings, "high", n - 1)
        if not level:
            return None
        if last_close > level["price"] and displacement_ok():
            anchor = last_swing_before(swings, "low", level["i"])
            anchor_p = (
                anchor["price"]
                if anchor
                else min(lows[max(0, level["i"] - 10) : level["i"]] or [lows[0]])
            )
            return level["price"], anchor_p, level["i"]
    else:
        level = last_swing_before(swings, "low", n - 1)
        if not level:
            return None
        if last_close < level["price"] and displacement_ok():
            anchor = last_swing_before(swings, "high", level["i"])
            anchor_p = (
                anchor["price"]
                if anchor
                else max(highs[max(0, level["i"] - 10) : level["i"]] or [highs[0]])
            )
            return level["price"], anchor_p, level["i"]
    return None


def find_order_blocks(opens, highs, lows, closes, bias, before_index, lookback=15, max_zones=3):
    start = max(0, before_index - lookback)
    zones = []
    for i in range(before_index - 1, start - 1, -1):
        is_bear = closes[i] < opens[i]
        is_bull = closes[i] > opens[i]
        if (bias == "bullish" and is_bear) or (bias == "bearish" and is_bull):
            zones.append({"high": highs[i], "low": lows[i], "type": "order_block"})
        if len(zones) >= max_zones:
            break
    return zones


def find_supply_demand_zone(
    opens, highs, lows, closes, bias, before_index, atr_val,
    consolidation_bars=3, move_atr_mult=1.5, lookback=30,
):
    if atr_val is None or np.isnan(atr_val) or consolidation_bars < 1:
        return None
    start = max(0, before_index - lookback)
    for end in range(before_index - 1, start + consolidation_bars, -1):
        bs = end - consolidation_bars
        bh, bl = highs[bs:end], lows[bs:end]
        if len(bh) == 0:
            continue
        if max(bh) - min(bl) > atr_val * 0.8:
            continue
        move = closes[end] - closes[bs]
        if bias == "bullish" and move >= move_atr_mult * atr_val:
            return {"high": max(bh), "low": min(bl), "type": "demand_zone"}
        if bias == "bearish" and move <= -move_atr_mult * atr_val:
            return {"high": max(bh), "low": min(bl), "type": "supply_zone"}
    return None


def find_liquidity_pools(swings, tolerance):
    pools = []
    for kind in ("high", "low"):
        pts = [s for s in swings if s["kind"] == kind]
        used = set()
        for i, s in enumerate(pts):
            if i in used:
                continue
            cluster = [s]
            for j in range(i + 1, len(pts)):
                if j in used:
                    continue
                if abs(pts[j]["price"] - s["price"]) <= tolerance:
                    cluster.append(pts[j])
                    used.add(j)
            if len(cluster) >= 2:
                pools.append(
                    {
                        "kind": kind,
                        "price": sum(c["price"] for c in cluster) / len(cluster),
                        "last_i": max(c["i"] for c in cluster),
                    }
                )
    return pools


def liquidity_swept_before_break(pools, bias, bos_index, lookback_bars):
    opp = "low" if bias == "bullish" else "high"
    for p in pools:
        if p["kind"] != opp:
            continue
        if p["last_i"] >= bos_index:
            continue
        if bos_index - p["last_i"] > lookback_bars:
            continue
        return True
    return False


def count_level_touches(highs, lows, level, tolerance, lookback_bars, before_index):
    start = max(0, before_index - lookback_bars)
    touches = 0
    in_touch = False
    for i in range(start, before_index):
        touching = lows[i] - tolerance <= level <= highs[i] + tolerance
        if touching and not in_touch:
            touches += 1
        in_touch = touching
    return touches


def is_engulfing(opens, closes, bias, i):
    if i < 1:
        return False
    o1, c1 = opens[i - 1], closes[i - 1]
    o2, c2 = opens[i], closes[i]
    if bias == "bullish":
        return c2 > o2 and o1 > c1 and c2 >= o1 and o2 <= c1
    return c2 < o2 and o1 < c1 and c2 <= o1 and o2 >= c1


def has_rejection_wick(opens, highs, lows, closes, bias, i, zone_low, zone_high, wick_ratio=0.5):
    rng = highs[i] - lows[i]
    if rng <= 0:
        return False
    body_low = min(opens[i], closes[i])
    body_high = max(opens[i], closes[i])
    if bias == "bullish":
        wick = body_low - lows[i]
        return lows[i] <= zone_high and (wick / rng) >= wick_ratio and closes[i] > body_low
    wick = highs[i] - body_high
    return highs[i] >= zone_low and (wick / rng) >= wick_ratio and closes[i] < body_high


def detect_fvg(opens, highs, lows, closes, i):
    if i < 2:
        return None
    if lows[i] > highs[i - 2] and closes[i - 1] > opens[i - 1]:
        return "bull"
    if highs[i] < lows[i - 2] and closes[i - 1] < opens[i - 1]:
        return "bear"
    return None


def in_session(ts, start_hour=9, end_hour=21):
    h = ts.hour
    if start_hour <= end_hour:
        return start_hour <= h < end_hour
    return h >= start_hour or h < end_hour


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_1m_data(path: str | None, start: str, end: str) -> pd.DataFrame:
    """Load 1-minute OHLC. Expects columns: datetime, open, high, low, close[, volume]"""
    if path and Path(path).exists():
        print(f"Loading local data from {path}")
        df = pd.read_csv(path)
        # flexible column names
        colmap = {c.lower(): c for c in df.columns}
        for need in ("open", "high", "low", "close"):
            if need not in colmap and need.title() in df.columns:
                colmap[need] = need.title()
        dt_col = next((c for c in df.columns if "time" in c.lower() or "date" in c.lower()), df.columns[0])
        df["datetime"] = pd.to_datetime(df[dt_col])
        df = df.set_index("datetime")[["open", "high", "low", "close"]].astype(float)
    else:
        # Fallback: try to use previously downloaded Histdata-style files if present
        data_dir = Path("data")
        files = sorted(data_dir.glob("DAT_ASCII_XAUUSD_M1_*.csv")) if data_dir.exists() else []
        if not files:
            print(
                "ERROR: No data provided.\n"
                "Pass --data path/to/1m.csv  or place Histdata CSVs in ./data/\n"
                "Expected format: datetime,open,high,low,close"
            )
            sys.exit(1)
        print(f"Loading {len(files)} Histdata files from ./data/")
        dfs = []
        for f in files:
            tmp = pd.read_csv(f, sep=";", header=None, names=["datetime", "open", "high", "low", "close", "volume"])
            tmp["datetime"] = pd.to_datetime(tmp["datetime"], format="%Y%m%d %H%M%S")
            dfs.append(tmp)
        df = pd.concat(dfs, ignore_index=True).drop_duplicates("datetime").set_index("datetime")
        df = df[["open", "high", "low", "close"]].astype(float)

    df = df.sort_index()
    if start:
        df = df[df.index >= pd.Timestamp(start)]
    if end:
        df = df[df.index <= pd.Timestamp(end)]
    print(f"1m bars: {len(df):,}   {df.index.min()} → {df.index.max()}")
    return df


# ---------------------------------------------------------------------------
# Backtest engine
# ---------------------------------------------------------------------------

def run_backtest(df1m: pd.DataFrame) -> pd.DataFrame:
    print("Resampling to 15min / 1H / 4H ...")
    ohlc = {"open": "first", "high": "max", "low": "min", "close": "last"}
    df15 = df1m.resample("15min").agg(ohlc).dropna()
    df1h = df1m.resample("1h").agg(ohlc).dropna()
    df4h = df1m.resample("4h").agg(ohlc).dropna()
    print(f"15m: {len(df15):,}   1H: {len(df1h):,}   4H: {len(df4h):,}")

    atr1h = atr(df1h["high"].values, df1h["low"].values, df1h["close"].values, ATR_PERIOD)
    atr15 = atr(df15["high"].values, df15["low"].values, df15["close"].values, ATR_PERIOD)

    trades = []
    open_trade = None
    active_zones = []
    last_bias = None
    setup = None

    times15 = df15.index
    opens15 = df15["open"].values
    highs15 = df15["high"].values
    lows15 = df15["low"].values
    closes15 = df15["close"].values
    n15 = len(df15)

    def htf_slice(df_htf, up_to, lookback=150):
        return df_htf.loc[df_htf.index <= up_to].tail(lookback)

    print("Running simulation ...")
    for i in range(100, n15):
        ts = times15[i]
        price = closes15[i]

        # ----- manage open trade (partial TPs) -----
        if open_trade is not None:
            t = open_trade
            remaining = t["remaining"]
            realized_r = t.get("realized_r", 0.0)
            hit_sl = False
            new_hits = []

            if t["side"] == "BUY":
                if lows15[i] <= t["sl"]:
                    hit_sl = True
                else:
                    for k, tp in enumerate(t["tps"]):
                        if k not in t["tps_hit"] and highs15[i] >= tp:
                            new_hits.append(k)
            else:
                if highs15[i] >= t["sl"]:
                    hit_sl = True
                else:
                    for k, tp in enumerate(t["tps"]):
                        if k not in t["tps_hit"] and lows15[i] <= tp:
                            new_hits.append(k)

            portion = 1.0 / len(t["tps"])
            for k in new_hits:
                t["tps_hit"].add(k)
                realized_r += portion * (k + 1)
                remaining -= portion
            t["realized_r"] = realized_r
            t["remaining"] = max(0.0, remaining)

            if hit_sl or t["remaining"] <= 1e-9:
                if hit_sl:
                    final_r = realized_r + t["remaining"] * (-1.0)
                    exit_price = t["sl"]
                    tp_str = ",".join(str(x + 1) for x in sorted(t["tps_hit"])) or "SL"
                else:
                    final_r = realized_r
                    exit_price = t["tps"][max(t["tps_hit"])] if t["tps_hit"] else t["entry"]
                    tp_str = ",".join(str(x + 1) for x in sorted(t["tps_hit"]))
                trades.append(
                    {
                        "entry_time": t["entry_time"],
                        "exit_time": ts,
                        "side": t["side"],
                        "entry": t["entry"],
                        "sl": t["sl"],
                        "exit": exit_price,
                        "r": final_r,
                        "tp_hit": tp_str,
                        "condition": t.get("condition", ""),
                        "duration_class": t.get("duration_class", "day"),
                        "bars_held": i - t["entry_i"],
                        "max_tp_reached": max(t["tps_hit"]) + 1 if t["tps_hit"] else 0,
                    }
                )
                open_trade = None
            else:
                open_trade = t

        # ----- 4H bias -----
        h4 = htf_slice(df4h, ts, 120)
        if len(h4) < 30:
            continue
        bias = get_bias(h4["high"].values, h4["low"].values, SWING_LOOKBACK)
        if bias is None:
            continue
        bias_flipped = last_bias is not None and last_bias != bias
        if bias_flipped:
            setup = None
            active_zones = [z for z in active_zones if z["direction"] == bias and z.get("active", True)]
        last_bias = bias

        # ----- 1H structure -----
        h1 = htf_slice(df1h, ts, 150)
        if len(h1) < 40:
            continue
        try:
            loc = df1h.index.get_loc(h1.index[-1])
            atr_s = atr1h[loc]
        except Exception:
            continue
        if atr_s is None or np.isnan(atr_s):
            continue

        bos = check_structure_break(
            h1["high"].values, h1["low"].values, h1["close"].values,
            bias, atr_s, DISPLACEMENT_ATR_MULT,
        )

        if bos:
            bos_level, pullback_zone, bos_index = bos
            is_new = setup is None or setup.get("bos_level") != bos_level
            if is_new:
                zones = find_order_blocks(
                    h1["open"].values, h1["high"].values, h1["low"].values, h1["close"].values,
                    bias, bos_index, OB_LOOKBACK, OB_MAX_ZONES,
                )
                sd = find_supply_demand_zone(
                    h1["open"].values, h1["high"].values, h1["low"].values, h1["close"].values,
                    bias, bos_index, atr_s, SD_CONSOLIDATION_BARS, SD_MOVE_ATR_MULT,
                )
                if sd:
                    zones.append(sd)

                swings = find_swings(h1["high"].values, h1["low"].values, SWING_LOOKBACK)
                tol = SR_TOUCH_TOLERANCE_ATR_MULT * atr_s
                pools = find_liquidity_pools(swings, tol)
                liquidity_ok = True
                if LIQUIDITY_LOOKBACK > 0:
                    liquidity_ok = liquidity_swept_before_break(pools, bias, bos_index, LIQUIDITY_LOOKBACK)

                sr_ok = True
                if SR_MIN_TOUCHES > 0 and zones:
                    level = zones[0]["low"] if bias == "bullish" else zones[0]["high"]
                    touches = count_level_touches(
                        h1["high"].values, h1["low"].values, level, tol, len(h1), bos_index
                    )
                    sr_ok = touches >= SR_MIN_TOUCHES

                prev_dir = setup.get("bias") if setup else None
                is_choch = bias_flipped or (prev_dir is not None and prev_dir != bias)

                for z in zones:
                    active_zones.append(
                        {
                            "high": z["high"],
                            "low": z["low"],
                            "type": z["type"],
                            "direction": bias,
                            "active": True,
                            "is_choch": is_choch,
                        }
                    )
                for d in ("bullish", "bearish"):
                    same = [z for z in active_zones if z["direction"] == d]
                    while len(same) > OB_MAX_ZONES:
                        active_zones.remove(same.pop(0))
                        same = [z for z in active_zones if z["direction"] == d]

                setup = {
                    "bos_level": bos_level,
                    "zones": zones,
                    "liquidity_ok": liquidity_ok,
                    "sr_ok": sr_ok,
                    "is_choch": is_choch,
                    "bias": bias,
                }

        # mitigation
        for z in active_zones:
            if not z.get("active", True):
                continue
            if z["direction"] == "bullish" and price < z["low"]:
                z["active"] = False
            elif z["direction"] == "bearish" and price > z["high"]:
                z["active"] = False

        if setup is None or open_trade is not None:
            continue
        if not setup.get("liquidity_ok", True) or not setup.get("sr_ok", True):
            continue
        if not in_session(ts, SESSION_START_UTC, SESSION_END_UTC):
            continue

        # ----- 15m confirmation -----
        active = [z for z in active_zones if z["direction"] == bias and z.get("active", True)]
        if not active:
            continue

        conf = None
        for zone in active:
            zlo, zhi = zone["low"], zone["high"]
            if not (lows15[i] <= zhi and highs15[i] >= zlo):
                continue
            engulf = is_engulfing(opens15, closes15, bias, i)
            rej = has_rejection_wick(
                opens15, highs15, lows15, closes15, bias, i, zlo, zhi, REJECTION_WICK_RATIO
            )
            fvg = detect_fvg(opens15, highs15, lows15, closes15, i)
            fvg_hit = (fvg == "bull" and bias == "bullish") or (fvg == "bear" and bias == "bearish")
            if REQUIRE_REJECTION and not rej:
                continue
            if not (engulf or rej or fvg_hit):
                continue
            conf = {
                "entry": closes15[i],
                "sl_anchor": zlo if bias == "bullish" else zhi,
                "zone_type": zone["type"],
                "confirmations": {"engulfing": engulf, "rejection": rej, "fvg": fvg_hit},
                "is_choch": zone.get("is_choch", False),
            }
            break
        if conf is None:
            continue

        # CHoCH preferred
        is_choch = conf.get("is_choch", False) or setup.get("is_choch", False)
        if CHOCH_PREFERRED and not is_choch:
            extra = (
                conf["confirmations"].get("fvg")
                or conf["confirmations"].get("engulfing")
                or setup.get("sr_ok", False)
            )
            if not extra:
                continue

        # build trade
        a15 = atr15[i] if i < len(atr15) and not np.isnan(atr15[i]) else atr_s
        buffer = SL_BUFFER_ATR_MULT * a15
        entry = conf["entry"]
        if bias == "bullish":
            sl = conf["sl_anchor"] - buffer
            r = entry - sl
            if r <= 0:
                continue
            tps = [entry + m * r for m in TP_MULTIPLES]
            side = "BUY"
        else:
            sl = conf["sl_anchor"] + buffer
            r = sl - entry
            if r <= 0:
                continue
            tps = [entry - m * r for m in TP_MULTIPLES]
            side = "SELL"

        score = 0
        if is_choch:
            score += 2
        score += 2  # displacement already required
        if conf["confirmations"].get("fvg"):
            score += 1
        if setup.get("liquidity_ok"):
            score += 1
        if conf["confirmations"].get("rejection"):
            score += 1
        if setup.get("sr_ok"):
            score += 1
        if conf["zone_type"] in ("demand_zone", "supply_zone"):
            score += 1
        dur = "swing" if score >= 5 else ("day_swing" if score >= 3 else "day")

        condition = f"{'CHoCH' if is_choch else 'BOS'}+{conf['zone_type']}"
        if conf["confirmations"].get("fvg"):
            condition += "+FVG"
        if conf["confirmations"].get("rejection"):
            condition += "+REJ"
        if conf["confirmations"].get("engulfing"):
            condition += "+ENG"

        open_trade = {
            "entry_time": ts,
            "entry_i": i,
            "side": side,
            "entry": entry,
            "sl": sl,
            "tps": tps,
            "r": r,
            "condition": condition,
            "duration_class": dur,
            "remaining": 1.0,
            "realized_r": 0.0,
            "tps_hit": set(),
        }
        setup = None

    # close leftover
    if open_trade is not None:
        t = open_trade
        exit_price = closes15[-1]
        mtm = (exit_price - t["entry"]) / t["r"] if t["side"] == "BUY" else (t["entry"] - exit_price) / t["r"]
        final_r = t.get("realized_r", 0.0) + t.get("remaining", 1.0) * mtm
        trades.append(
            {
                "entry_time": t["entry_time"],
                "exit_time": times15[-1],
                "side": t["side"],
                "entry": t["entry"],
                "sl": t["sl"],
                "exit": exit_price,
                "r": final_r,
                "tp_hit": ",".join(str(x + 1) for x in sorted(t.get("tps_hit", []))) or "OPEN",
                "condition": t.get("condition", ""),
                "duration_class": t.get("duration_class", "day"),
                "bars_held": n15 - 1 - t["entry_i"],
                "max_tp_reached": max(t["tps_hit"]) + 1 if t.get("tps_hit") else 0,
            }
        )

    return pd.DataFrame(trades)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_and_save_summary(trades: pd.DataFrame, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_lines = []

    def log(msg=""):
        print(msg)
        summary_lines.append(msg)

    log("=== BACKTEST SUMMARY ===")
    log(f"Total trades : {len(trades)}")
    if len(trades) == 0:
        log("No trades generated.")
        (out_dir / "backtest_summary.txt").write_text("\n".join(summary_lines))
        return

    wins = trades[trades["r"] > 0]
    losses = trades[trades["r"] <= 0]
    log(f"Win rate     : {len(wins)/len(trades)*100:.1f}%  ({len(wins)}/{len(trades)})")
    log(f"Average R    : {trades['r'].mean():.2f}")
    log(f"Median R     : {trades['r'].median():.2f}")
    pf = wins["r"].sum() / abs(losses["r"].sum()) if len(losses) and losses["r"].sum() != 0 else float("inf")
    log(f"Profit factor: {pf:.2f}")
    log(f"Total R      : {trades['r'].sum():.1f}")
    log(f"Max R / Min R: {trades['r'].max():.2f} / {trades['r'].min():.2f}")

    equity = trades["r"].cumsum()
    dd = equity - equity.cummax()
    log(f"Max Drawdown : {dd.min():.1f} R")

    log("\nBy conviction class:")
    log(str(trades.groupby("duration_class")["r"].agg(["count", "mean", "sum"])))

    log("\nTP hit distribution:")
    log(str(trades["tp_hit"].value_counts(dropna=False).sort_index()))

    trades.to_csv(out_dir / "backtest_trades.csv", index=False)
    log(f"\nTrade list → {out_dir / 'backtest_trades.csv'}")

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(12, 5))
        ax.plot(equity.values, label="Cumulative R")
        ax.fill_between(range(len(dd)), dd.values, 0, alpha=0.3, color="red", label="Drawdown")
        ax.set_title("Swing SMC Backtest – Equity Curve")
        ax.set_xlabel("Trade #")
        ax.set_ylabel("R")
        ax.legend()
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(out_dir / "backtest_equity.png", dpi=120)
        log(f"Equity curve → {out_dir / 'backtest_equity.png'}")
    except Exception as e:
        log(f"Plot failed: {e}")

    (out_dir / "backtest_summary.txt").write_text("\n".join(summary_lines))
    log(f"Summary     → {out_dir / 'backtest_summary.txt'}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backtest Swing STRUCTURE (SMC) strategy")
    parser.add_argument("--pair", default="XAU/USD", help="Symbol (for reporting only)")
    parser.add_argument("--start", default="2024-01-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: latest data)")
    parser.add_argument("--data", default=None, help="Path to 1-minute OHLC CSV")
    parser.add_argument("--out", default="backtest_results", help="Output directory")
    args = parser.parse_args()

    df1m = load_1m_data(args.data, args.start, args.end)
    trades = run_backtest(df1m)
    print_and_save_summary(trades, Path(args.out))


if __name__ == "__main__":
    main()
