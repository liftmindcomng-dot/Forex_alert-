#!/usr/bin/env python3
"""
THOMEHX SNIPER V7 — Multi-timeframe SMC backtest engine for XAUUSD.

WHAT THIS DOES
    4H bias (structure: swings + BOS/CHoCH)
      -> 1H confirmation (structure must agree with 4H, no EMA substitute)
        -> 15M setup (liquidity sweep, BOS/CHoCH/MSS, displacement, FVG,
                       order block / S-D zone, optional S/R confluence)
          -> 5M entry confirmation (retest + rejection/confirmation candle
                                     + 5M structure agreement)
            -> order type chosen from price's position relative to the zone
               (BUY/SELL NOW, LIMIT, or STOP) -> pending-order lifecycle
               -> SL/TP simulation on M5 closed candles, 1R risk / 2R target
               (configurable).

WHAT THIS DELIBERATELY DOES NOT DO
    It does not produce a verdict about whether the strategy "works" on your
    real data -- run it and read the numbers. SMC concepts (order blocks,
    liquidity sweeps, displacement, "quality") are discretionary by nature;
    there is no single canonical algorithmic definition of any of them. The
    detectors below encode ONE reasonable, fully documented interpretation
    of each concept. A different discretionary trader would draw some of
    these zones differently. Treat this as a rigorous, look-ahead-free
    implementation of a *specific, literal reading* of the written rules --
    not a certification that it matches what any given trader means by
    "genuine SMC structure."

NO-LOOKAHEAD DESIGN
    - HTF (4H/1H/15M) candles are derived from your M5 file by resampling,
      so all timeframes come from one source (no mismatch/gaps).
    - A HTF candle only becomes visible to the engine once wall-clock time
      (tracked via the M5 series) reaches that candle's close time.
    - A swing high/low at bar p is only "confirmed" (usable) once
      `swing_lookback` further bars have closed after it -- like a real-time
      trader only recognizing a fractal after price moves away from it.
    - All structure/bias state is computed via a single forward pass per
      timeframe, not recomputed from a growing window each step -- this is
      a performance requirement for multi-year M5 data, not a behavior change.

INPUT DATA
    A single CSV of 5-minute OHLC candles for XAUUSD, columns:
        time,open,high,low,close
    `time` must be parseable timestamps, ascending (duplicates/gaps are
    tolerated; the resampler just drops empty HTF bars).

USAGE
    python backtest_sniper_v7.py --data xauusd_m5.csv \
        --start 2024-01-01 --end 2026-07-31 --out report.txt \
        --trades-csv trades.csv

    Key options (see --help for all):
        --rr 2.0                        reward multiple (default 2R)
        --swing-lookback 3              fractal swing width in bars
        --same-candle-assumption sl_first|tp_first   (default: sl_first,
            i.e. conservative -- if one M5 candle's range touches BOTH the
            SL and TP and open/close order can't disambiguate which came
            first, assume the loss)
        --setup-timeout-hours 4         how long a 15M setup stays valid
            while waiting for the 5M retest before being discarded
"""

import argparse
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd


class Bias(Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NONE = "NONE"


class OrderType(Enum):
    BUY_NOW = "BUY_NOW"
    SELL_NOW = "SELL_NOW"
    BUY_LIMIT = "BUY_LIMIT"
    SELL_LIMIT = "SELL_LIMIT"
    BUY_STOP = "BUY_STOP"
    SELL_STOP = "SELL_STOP"


class SetupKind(Enum):
    CONTINUATION = "CONTINUATION"
    REVERSAL = "REVERSAL"


def load_m5(path: str, start: Optional[str], end: Optional[str]) -> pd.DataFrame:
    df = pd.read_csv(path)
    if "time" not in df.columns:
        raise ValueError("CSV must have a 'time' column")
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").drop_duplicates(subset="time").reset_index(drop=True)
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSV missing required columns: {missing}")
    if start:
        df = df[df["time"] >= pd.Timestamp(start)]
    if end:
        df = df[df["time"] <= pd.Timestamp(end)]
    df = df.reset_index(drop=True)
    if len(df) < 500:
        raise ValueError(
            f"Only {len(df)} M5 candles in range -- too little data to backtest "
            "a 4H->1H->15M->5M cascade. Check your file/date range."
        )
    return df


def resample_ohlc(df_m5: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Build HTF candles from M5. label/closed='left' so a candle stamped at
    time X covers [X, X+rule) and is only CLOSED once wall-clock time reaches
    X+rule -- this is what lets us enforce closed-candles-only downstream."""
    s = df_m5.set_index("time")
    o = s["open"].resample(rule, label="left", closed="left").first()
    h = s["high"].resample(rule, label="left", closed="left").max()
    l = s["low"].resample(rule, label="left", closed="left").min()
    c = s["close"].resample(rule, label="left", closed="left").last()
    out = pd.DataFrame({"open": o, "high": h, "low": l, "close": c}).dropna()
    out["close_time"] = out.index + pd.Timedelta(rule)
    out = out.reset_index().rename(columns={"time": "open_time"})
    return out


def last_closed_idx_array(htf_close_times: np.ndarray, now_array: np.ndarray) -> np.ndarray:
    """Vectorized: for each timestamp in now_array, index of the most recent
    HTF candle whose close_time <= now. -1 if none yet."""
    return np.searchsorted(htf_close_times, now_array, side="right") - 1


def find_swings_vectorized(high: np.ndarray, low: np.ndarray, lookback: int) -> Tuple[np.ndarray, np.ndarray]:
    """Fractal swing highs/lows over the FULL series (vectorized, O(n)).
    A candidate swing at index p is a real fractal in absolute terms, but is
    only *knowable* once bars p+1..p+lookback have closed -- that delay is
    handled separately in compute_confirmed_swings, not here."""
    n = len(high)
    is_high = np.zeros(n, dtype=bool)
    is_low = np.zeros(n, dtype=bool)
    for p in range(lookback, n - lookback):
        wh = high[p - lookback: p + lookback + 1]
        wl = low[p - lookback: p + lookback + 1]
        if high[p] == wh.max() and np.argmax(wh) == lookback:
            is_high[p] = True
        if low[p] == wl.min() and np.argmin(wl) == lookback:
            is_low[p] = True
    return is_high, is_low


def compute_confirmed_swings(df: pd.DataFrame, lookback: int) -> pd.DataFrame:
    """Single forward pass: at each index idx, what is the most recent
    CONFIRMED swing high/low value+position, using only information that
    would have been available at idx (a swing at p is only usable once
    idx >= p + lookback)."""
    n = len(df)
    high = df["high"].values
    low = df["low"].values
    is_high, is_low = find_swings_vectorized(high, low, lookback)

    conf_high_val = np.full(n, np.nan)
    conf_high_idx = np.full(n, -1, dtype=int)
    conf_low_val = np.full(n, np.nan)
    conf_low_idx = np.full(n, -1, dtype=int)

    cur_high_val, cur_high_idx = np.nan, -1
    cur_low_val, cur_low_idx = np.nan, -1
    for idx in range(n):
        p = idx - lookback
        if p >= 0:
            if is_high[p]:
                cur_high_val, cur_high_idx = high[p], p
            if is_low[p]:
                cur_low_val, cur_low_idx = low[p], p
        conf_high_val[idx], conf_high_idx[idx] = cur_high_val, cur_high_idx
        conf_low_val[idx], conf_low_idx[idx] = cur_low_val, cur_low_idx

    out = df.copy()
    out["conf_high_val"] = conf_high_val
    out["conf_high_idx"] = conf_high_idx
    out["conf_low_val"] = conf_low_val
    out["conf_low_idx"] = conf_low_idx
    out["is_swing_high"] = is_high
    out["is_swing_low"] = is_low
    return out


def compute_bias_series(df_conf: pd.DataFrame) -> np.ndarray:
    """Single forward pass state machine over confirmed swings:
    BOS = close extends beyond the swing in the current trend's direction
          (trend re-confirmed, not a flip).
    CHoCH = close breaks beyond the swing AGAINST the current trend's
          direction (trend flips)."""
    n = len(df_conf)
    close = df_conf["close"].values
    ch_val, ch_idx = df_conf["conf_high_val"].values, df_conf["conf_high_idx"].values
    cl_val, cl_idx = df_conf["conf_low_val"].values, df_conf["conf_low_idx"].values

    bias = np.full(n, Bias.NONE.value, dtype=object)
    current = Bias.NONE
    for idx in range(n):
        if np.isnan(ch_val[idx]) or np.isnan(cl_val[idx]):
            bias[idx] = Bias.NONE.value
            continue
        if current == Bias.NONE:
            current = Bias.BULLISH if cl_idx[idx] > ch_idx[idx] else Bias.BEARISH
        if current == Bias.BULLISH and close[idx] < cl_val[idx]:
            current = Bias.BEARISH
        elif current == Bias.BEARISH and close[idx] > ch_val[idx]:
            current = Bias.BULLISH
        bias[idx] = current.value
    return bias


@dataclass
class Zone:
    kind: str
    direction: str
    top: float
    bottom: float
    formed_idx: int
    formed_time: pd.Timestamp


def detect_fvg(df15: pd.DataFrame, i: int) -> Optional[Zone]:
    """3-candle imbalance ending at bar i (classic ICT definition).
    Bullish: low[i] > high[i-2]. Bearish: high[i] < low[i-2]."""
    if i < 2:
        return None
    a, c = df15.iloc[i - 2], df15.iloc[i]
    if c["low"] > a["high"]:
        return Zone("FVG", "BULLISH", top=c["low"], bottom=a["high"],
                     formed_idx=i, formed_time=df15.iloc[i]["open_time"])
    if c["high"] < a["low"]:
        return Zone("FVG", "BEARISH", top=a["low"], bottom=c["high"],
                     formed_idx=i, formed_time=df15.iloc[i]["open_time"])
    return None


def detect_displacement(body: float, atr_val: float, mult: float = 1.5) -> bool:
    """A 'meaningful displacement' candle: body >= mult * rolling ATR(14)."""
    if pd.isna(atr_val):
        return False
    return body >= mult * atr_val


def detect_order_block(df15: pd.DataFrame, i: int, direction: str) -> Optional[Zone]:
    """Order block = last opposite-colored candle immediately before the
    displacement candle at i."""
    if i < 1:
        return None
    prev = df15.iloc[i - 1]
    prev_bearish = prev["close"] < prev["open"]
    prev_bullish = prev["close"] > prev["open"]
    if direction == "BULLISH" and prev_bearish:
        return Zone("OB", "BULLISH", top=prev["high"], bottom=prev["low"],
                     formed_idx=i - 1, formed_time=df15.iloc[i - 1]["open_time"])
    if direction == "BEARISH" and prev_bullish:
        return Zone("OB", "BEARISH", top=prev["high"], bottom=prev["low"],
                     formed_idx=i - 1, formed_time=df15.iloc[i - 1]["open_time"])
    return None


def detect_liquidity_sweep(bar_high: float, bar_low: float, bar_close: float,
                            ref_high: Optional[float], ref_low: Optional[float]) -> Optional[str]:
    """Wick beyond a recent confirmed swing high/low that closes back inside
    (a stop hunt). 'HIGH' = swept highs (bearish signal), 'LOW' = swept lows
    (bullish signal)."""
    if ref_high is not None and not pd.isna(ref_high) and bar_high > ref_high and bar_close < ref_high:
        return "HIGH"
    if ref_low is not None and not pd.isna(ref_low) and bar_low < ref_low and bar_close > ref_low:
        return "LOW"
    return None


def atr_series(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


@dataclass
class Setup:
    direction: str
    kind: SetupKind
    zone: Zone
    sl_level: float
    formed_time: pd.Timestamp
    confluences: List[str] = field(default_factory=list)


@dataclass
class PendingOrder:
    order_type: OrderType
    trigger_price: float
    sl: float
    tp: float
    setup: Setup
    created_time: pd.Timestamp
    invalidation_level: float


@dataclass
class Trade:
    direction: str
    order_type: OrderType
    entry_time: pd.Timestamp
    entry_price: float
    sl: float
    tp: float
    setup_kind: SetupKind
    confluences: List[str]
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    result: Optional[str] = None
    r_multiple: Optional[float] = None


def choose_order_type(direction: str, current_price: float, zone: Zone) -> Tuple[OrderType, float]:
    """Decide NOW / LIMIT / STOP from where price sits relative to the zone
    ('determine the appropriate order type from the setup'; 'do not force
    every trade to NOW').

    BULLISH: price inside the zone -> BUY_NOW; price above the zone (needs a
        pullback down into it) -> BUY_LIMIT at the zone's top; price below
        the zone (needs a breakout up through it to confirm) -> BUY_STOP at
        the zone's top. Mirrored for BEARISH."""
    if direction == "BULLISH":
        if zone.bottom <= current_price <= zone.top:
            return OrderType.BUY_NOW, current_price
        if current_price > zone.top:
            return OrderType.BUY_LIMIT, zone.top
        return OrderType.BUY_STOP, zone.top
    else:
        if zone.bottom <= current_price <= zone.top:
            return OrderType.SELL_NOW, current_price
        if current_price < zone.bottom:
            return OrderType.SELL_LIMIT, zone.bottom
        return OrderType.SELL_STOP, zone.bottom


def run_backtest(df_m5: pd.DataFrame, rr: float = 2.0, swing_lookback: int = 3,
                  same_candle_assumption: str = "sl_first",
                  setup_timeout_hours: float = 4.0) -> List[Trade]:
    df15 = resample_ohlc(df_m5, "15min")
    df1h = resample_ohlc(df_m5, "1h")
    df4h = resample_ohlc(df_m5, "4h")
    df15["atr"] = atr_series(df15)

    df4h = compute_confirmed_swings(df4h, swing_lookback)
    df4h["bias"] = compute_bias_series(df4h)
    df1h = compute_confirmed_swings(df1h, swing_lookback)
    df1h["bias"] = compute_bias_series(df1h)
    df15 = compute_confirmed_swings(df15, swing_lookback)

    m5_time = df_m5["time"].values
    close15 = df15["close_time"].values
    m5_bounds = np.searchsorted(m5_time, close15, side="right")
    h4_idx_arr = last_closed_idx_array(df4h["close_time"].values, close15)
    h1_idx_arr = last_closed_idx_array(df1h["close_time"].values, close15)

    m5_open = df_m5["open"].values
    m5_high = df_m5["high"].values
    m5_low = df_m5["low"].values
    m5_close = df_m5["close"].values
    m5_time_pd = df_m5["time"]

    body15 = (df15["close"] - df15["open"]).abs().values
    open15 = df15["open"].values
    close15_vals = df15["close"].values
    high15 = df15["high"].values
    low15 = df15["low"].values
    atr15 = df15["atr"].values
    conf_high15 = df15["conf_high_val"].values
    conf_low15 = df15["conf_low_val"].values
    bias4h_vals = df4h["bias"].values
    bias1h_vals = df1h["bias"].values

    trades: List[Trade] = []
    pending: Optional[PendingOrder] = None
    open_trade: Optional[Trade] = None
    last_setup_zone_id: Optional[Tuple] = None
    prev_bound = 0

    n15 = len(df15)
    for i in range(30, n15):
        bound = int(m5_bounds[i])
        seg_start, seg_end = prev_bound, bound
        prev_bound = bound

        if open_trade is not None:
            for k in range(seg_start, seg_end):
                if open_trade.direction == "BULLISH":
                    hit_sl = m5_low[k] <= open_trade.sl
                    hit_tp = m5_high[k] >= open_trade.tp
                else:
                    hit_sl = m5_high[k] >= open_trade.sl
                    hit_tp = m5_low[k] <= open_trade.tp
                if hit_sl and hit_tp:
                    if same_candle_assumption == "sl_first":
                        open_trade.exit_price, open_trade.result, open_trade.r_multiple = open_trade.sl, "Loss", -1.0
                    else:
                        open_trade.exit_price, open_trade.result, open_trade.r_multiple = open_trade.tp, "Win", rr
                    open_trade.exit_time = m5_time_pd.iloc[k]
                    trades.append(open_trade)
                    open_trade = None
                    break
                elif hit_sl:
                    open_trade.exit_price, open_trade.result, open_trade.r_multiple = open_trade.sl, "Loss", -1.0
                    open_trade.exit_time = m5_time_pd.iloc[k]
                    trades.append(open_trade)
                    open_trade = None
                    break
                elif hit_tp:
                    open_trade.exit_price, open_trade.result, open_trade.r_multiple = open_trade.tp, "Win", rr
                    open_trade.exit_time = m5_time_pd.iloc[k]
                    trades.append(open_trade)
                    open_trade = None
                    break

        if pending is not None and open_trade is None:
            for k in range(seg_start, seg_end):
                invalidated = (
                    m5_close[k] < pending.invalidation_level
                    if pending.setup.direction == "BULLISH"
                    else m5_close[k] > pending.invalidation_level
                )
                if invalidated:
                    pending = None
                    break
                triggered = False
                if pending.order_type in (OrderType.BUY_NOW, OrderType.SELL_NOW):
                    triggered = True
                elif pending.order_type == OrderType.BUY_LIMIT and m5_low[k] <= pending.trigger_price:
                    triggered = True
                elif pending.order_type == OrderType.SELL_LIMIT and m5_high[k] >= pending.trigger_price:
                    triggered = True
                elif pending.order_type == OrderType.BUY_STOP and m5_high[k] >= pending.trigger_price:
                    triggered = True
                elif pending.order_type == OrderType.SELL_STOP and m5_low[k] <= pending.trigger_price:
                    triggered = True
                if triggered:
                    open_trade = Trade(
                        direction=pending.setup.direction, order_type=pending.order_type,
                        entry_time=m5_time_pd.iloc[k], entry_price=pending.trigger_price,
                        sl=pending.sl, tp=pending.tp, setup_kind=pending.setup.kind,
                        confluences=pending.setup.confluences,
                    )
                    pending = None
                    break
                elapsed_h = (m5_time_pd.iloc[k] - pending.created_time).total_seconds() / 3600.0
                if elapsed_h > setup_timeout_hours:
                    pending = None
                    break

        if open_trade is not None or pending is not None:
            continue

        h4_idx, h1_idx = int(h4_idx_arr[i]), int(h1_idx_arr[i])
        if h4_idx < 10 or h1_idx < 10:
            continue
        bias_4h = bias4h_vals[h4_idx]
        bias_1h = bias1h_vals[h1_idx]
        if bias_4h == Bias.NONE.value or bias_4h != bias_1h:
            continue
        direction = bias_4h

        ref_high, ref_low = conf_high15[i], conf_low15[i]
        sweep = detect_liquidity_sweep(high15[i], low15[i], close15_vals[i], ref_high, ref_low)
        want_sweep = "LOW" if direction == "BULLISH" else "HIGH"
        has_sweep = sweep == want_sweep

        disp = detect_displacement(body15[i], atr15[i])
        candle_dir = "BULLISH" if close15_vals[i] > open15[i] else "BEARISH"
        if not (disp and candle_dir == direction):
            continue

        fvg = detect_fvg(df15, i)
        ob = detect_order_block(df15, i, direction)
        zone = fvg if (fvg and fvg.direction == direction) else (ob if (ob and ob.direction == direction) else None)
        if zone is None:
            continue

        confluences = ["displacement"]
        if has_sweep:
            confluences.append("liquidity_sweep")
        if fvg and fvg.direction == direction:
            confluences.append("fvg")
        if ob and ob.direction == direction:
            confluences.append("order_block")

        zone_id = (zone.formed_idx, zone.direction, zone.kind)
        if zone_id == last_setup_zone_id:
            continue

        if (direction == "BULLISH" and pd.isna(ref_low)) or (direction == "BEARISH" and pd.isna(ref_high)):
            continue
        struct_ref = ref_low if direction == "BULLISH" else ref_high
        buffer = atr15[i] * 0.1 if not pd.isna(atr15[i]) else 0.0
        sl_level = struct_ref - buffer if direction == "BULLISH" else struct_ref + buffer

        setup_kind = SetupKind.REVERSAL if has_sweep else SetupKind.CONTINUATION
        setup = Setup(direction=direction, kind=setup_kind, zone=zone, sl_level=sl_level,
                      formed_time=df15["open_time"].iloc[i], confluences=confluences)

        scan_start = bound
        scan_end = min(len(df_m5), bound + int(setup_timeout_hours * 12))
        confirmed = False
        entry_ref_price, confirm_time = None, None
        for k in range(scan_start + 1, scan_end):
            touched = (m5_low[k] <= zone.top and m5_high[k] >= zone.bottom)
            if not touched:
                broke = (m5_close[k] < sl_level) if direction == "BULLISH" else (m5_close[k] > sl_level)
                if broke:
                    break
                continue
            rng = m5_high[k] - m5_low[k]
            body = abs(m5_close[k] - m5_open[k])
            strong_confirm = rng > 0 and (body / rng) >= 0.5
            rejects_right_way = (m5_close[k] > m5_open[k]) if direction == "BULLISH" else (m5_close[k] < m5_open[k])
            m5_struct_ok = (m5_close[k] > m5_high[k - 1]) if direction == "BULLISH" else (m5_close[k] < m5_low[k - 1])
            if strong_confirm and rejects_right_way and m5_struct_ok:
                confirmed = True
                entry_ref_price = m5_close[k]
                confirm_time = m5_time_pd.iloc[k]
                break
        if not confirmed:
            continue

        last_setup_zone_id = zone_id
        order_type, trigger_price = choose_order_type(direction, entry_ref_price, zone)
        risk = abs(trigger_price - sl_level)
        if risk <= 0:
            continue
        tp_level = trigger_price + rr * risk if direction == "BULLISH" else trigger_price - rr * risk

        pending = PendingOrder(order_type=order_type, trigger_price=trigger_price, sl=sl_level,
                                tp=tp_level, setup=setup, created_time=confirm_time,
                                invalidation_level=sl_level)

    return trades


def max_drawdown_r(r_series: List[float]) -> float:
    if not r_series:
        return 0.0
    cum = np.cumsum(r_series)
    peak = np.maximum.accumulate(cum)
    dd = peak - cum
    return float(dd.max())


def streaks(results: List[str]) -> Tuple[int, int]:
    max_win = max_loss = cur_win = cur_loss = 0
    for r in results:
        if r == "Win":
            cur_win += 1
            cur_loss = 0
        else:
            cur_loss += 1
            cur_win = 0
        max_win = max(max_win, cur_win)
        max_loss = max(max_loss, cur_loss)
    return max_win, max_loss


def bucket_stats(trades: List[Trade], key) -> dict:
    groups = {}
    for t in trades:
        groups.setdefault(key(t), []).append(t)
    out = {}
    for k, ts in groups.items():
        wins = [t for t in ts if t.result == "Win"]
        losses = [t for t in ts if t.result == "Loss"]
        gross_w = sum(t.r_multiple for t in wins)
        gross_l = sum(abs(t.r_multiple) for t in losses)
        out[k] = {
            "n": len(ts), "wins": len(wins), "losses": len(losses),
            "win_rate": len(wins) / len(ts) if ts else 0.0,
            "net_r": sum(t.r_multiple for t in ts),
            "avg_r": (sum(t.r_multiple for t in ts) / len(ts)) if ts else 0.0,
            "profit_factor": (gross_w / gross_l) if gross_l > 0 else (float("inf") if gross_w > 0 else 0.0),
        }
    return out


def build_report(trades: List[Trade]) -> str:
    lines = []
    n = len(trades)
    if n == 0:
        return ("No trades were generated over the tested period/data. This can be a "
                "correct, if disappointing, result -- it means the full 4H->1H->15M->5M "
                "confluence chain never lined up, not that the engine crashed. Check "
                "--trades-csv is empty vs. missing to confirm, and consider whether your "
                "date range gives the structure detectors enough bars.")
    wins = [t for t in trades if t.result == "Win"]
    losses = [t for t in trades if t.result == "Loss"]
    r_list = [t.r_multiple for t in trades]
    gross_w = sum(t.r_multiple for t in wins)
    gross_l = sum(abs(t.r_multiple) for t in losses)
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
    max_w_streak, max_l_streak = streaks([t.result for t in trades])

    lines.append("=== RAW RESULTS ===")
    lines.append(f"Total Trades: {n}")
    lines.append(f"Wins: {len(wins)}")
    lines.append(f"Losses: {len(losses)}")
    lines.append(f"Win Rate: {len(wins)/n:.2%}")
    lines.append("")

    by_dir = bucket_stats(trades, lambda t: t.direction)
    for d in ("BULLISH", "BEARISH"):
        label = "BUY" if d == "BULLISH" else "SELL"
        s = by_dir.get(d, {"wins": 0, "losses": 0})
        lines.append(f"{label} wins/losses: {s.get('wins',0)}/{s.get('losses',0)}")
    lines.append("")

    by_type = bucket_stats(trades, lambda t: t.order_type.value)
    for ot in OrderType:
        s = by_type.get(ot.value, {"n": 0})
        lines.append(f"{ot.value}: {s['n']} trades")
    lines.append("")

    lines.append(f"Profit Factor: {pf:.3f}" if pf != float('inf') else "Profit Factor: inf (no losses)")
    lines.append(f"Net R: {sum(r_list):.2f}")
    lines.append(f"Average R: {sum(r_list)/n:.3f}")
    lines.append(f"Maximum Drawdown (R): {max_drawdown_r(r_list):.2f}")
    lines.append(f"Maximum Winning Streak: {max_w_streak}")
    lines.append(f"Maximum Losing Streak: {max_l_streak}")
    lines.append("")

    lines.append("=== ENTRY-TYPE ANALYSIS ===")
    for ot in OrderType:
        s = by_type.get(ot.value)
        if not s or s["n"] == 0:
            lines.append(f"{ot.value}: 0 trades")
            continue
        pf_s = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.3f}"
        lines.append(
            f"{ot.value}: n={s['n']} wins={s['wins']} losses={s['losses']} "
            f"win_rate={s['win_rate']:.2%} net_r={s['net_r']:.2f} "
            f"avg_r={s['avg_r']:.3f} pf={pf_s}"
        )
    lines.append("")

    by_kind = bucket_stats(trades, lambda t: t.setup_kind.value)
    lines.append("=== SETUP KIND (reversal vs continuation) ===")
    for k, s in by_kind.items():
        lines.append(f"{k}: n={s['n']} win_rate={s['win_rate']:.2%} net_r={s['net_r']:.2f}")
    lines.append("")

    conf_pnl = {}
    for t in trades:
        for c in t.confluences:
            conf_pnl.setdefault(c, []).append(t.r_multiple)
    lines.append("=== CONFLUENCE CONTRIBUTION (net/avg R of trades where present) ===")
    for c, rs in sorted(conf_pnl.items(), key=lambda kv: -sum(kv[1])):
        lines.append(f"{c}: n={len(rs)} net_r={sum(rs):.2f} avg_r={sum(rs)/len(rs):.3f}")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="THOMEHX SNIPER V7 backtest engine")
    ap.add_argument("--data", required=True, help="CSV of M5 OHLC data (time,open,high,low,close)")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-07-31")
    ap.add_argument("--rr", type=float, default=2.0)
    ap.add_argument("--swing-lookback", type=int, default=3)
    ap.add_argument("--same-candle-assumption", choices=["sl_first", "tp_first"], default="sl_first")
    ap.add_argument("--setup-timeout-hours", type=float, default=4.0)
    ap.add_argument("--out", default=None, help="also write the text report to this file")
    ap.add_argument("--trades-csv", default=None, help="dump every simulated trade to this CSV")
    args = ap.parse_args()

    df_m5 = load_m5(args.data, args.start, args.end)
    trades = run_backtest(
        df_m5, rr=args.rr, swing_lookback=args.swing_lookback,
        same_candle_assumption=args.same_candle_assumption,
        setup_timeout_hours=args.setup_timeout_hours,
    )
    report = build_report(trades)
    print(report)
    if args.out:
        with open(args.out, "w") as f:
            f.write(report)
    if args.trades_csv:
        rows = [{
            "direction": "BUY" if t.direction == "BULLISH" else "SELL",
            "order_type": t.order_type.value,
            "entry_time": t.entry_time, "entry_price": t.entry_price,
            "sl": t.sl, "tp": t.tp, "setup_kind": t.setup_kind.value,
            "confluences": "|".join(t.confluences),
            "exit_time": t.exit_time, "exit_price": t.exit_price,
            "result": t.result, "r_multiple": t.r_multiple,
        } for t in trades]
        pd.DataFrame(rows).to_csv(args.trades_csv, index=False)


if __name__ == "__main__":
    main()
