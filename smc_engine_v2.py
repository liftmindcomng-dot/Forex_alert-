"""
smc_engine_v2.py
Full SMC swing engine - order blocks with persistent mitigation tracking,
fair value gaps, liquidity sweeps, displacement, rejection, 3-touch
support/resistance, supply/demand zones, a condition label, and a
day-vs-swing duration classification. Alert-only - no auto-trade logic.
"""

import math


# ==================== BASIC INDICATORS ====================
def atr(candles, length=14):
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
    if len(trs) < length:
        return sum(trs) / len(trs) if trs else 0.0
    return sum(trs[-length:]) / length


def swing_points(candles, lookback):
    points = []
    for i in range(lookback, len(candles) - lookback):
        window = candles[i - lookback:i + lookback + 1]
        if candles[i]["high"] == max(c["high"] for c in window):
            points.append((i, candles[i]["high"], "high"))
        if candles[i]["low"] == min(c["low"] for c in window):
            points.append((i, candles[i]["low"], "low"))
    return points


# ==================== STRUCTURE (BOS/CHoCH) ====================
def detect_structure_break(candles, lookback, prior_trend):
    """
    Returns (new_trend, break_info). break_info is None if no break,
    otherwise {"direction", "bos_price", "bos_index", "is_choch"}.
    is_choch = True when the break reverses the prior trend (CHoCH),
    False when it continues the existing trend (BOS).
    """
    points = swing_points(candles, lookback)
    last_high = next((p for p in reversed(points) if p[2] == "high"), None)
    last_low = next((p for p in reversed(points) if p[2] == "low"), None)

    c = candles[-1]
    if last_high and c["close"] > last_high[1]:
        is_choch = prior_trend == "bear"
        return "bull", {"direction": "bull", "bos_price": last_high[1],
                         "bos_index": len(candles) - 1, "is_choch": is_choch}
    if last_low and c["close"] < last_low[1]:
        is_choch = prior_trend == "bull"
        return "bear", {"direction": "bear", "bos_price": last_low[1],
                         "bos_index": len(candles) - 1, "is_choch": is_choch}
    return prior_trend, None


# ==================== ORDER BLOCKS (persistent, mitigation-tracked, tagged BOS/CHoCH) ====================
def build_order_block(candles, break_index, direction, is_choch):
    top, bot = candles[break_index - 1]["high"], candles[break_index - 1]["low"]
    for j in range(break_index - 1, max(break_index - 6, 0), -1):
        c = candles[j]
        if direction == "bull" and c["close"] < c["open"]:
            top, bot = c["high"], c["low"]
            break
        if direction == "bear" and c["close"] > c["open"]:
            top, bot = c["high"], c["low"]
            break
    return {"direction": direction, "top": top, "bot": bot, "active": True,
            "created_index": break_index, "is_choch": is_choch}


def update_order_blocks(order_blocks, current_price, max_zones=5):
    inside_bull = inside_bear = False
    active_bull_zone = active_bear_zone = None

    for ob in order_blocks:
        if not ob["active"]:
            continue
        if ob["direction"] == "bull" and current_price < ob["bot"]:
            ob["active"] = False
        elif ob["direction"] == "bear" and current_price > ob["top"]:
            ob["active"] = False
        elif ob["bot"] <= current_price <= ob["top"]:
            if ob["direction"] == "bull":
                inside_bull = True
                active_bull_zone = ob
            else:
                inside_bear = True
                active_bear_zone = ob

    for direction in ("bull", "bear"):
        same_dir = [o for o in order_blocks if o["direction"] == direction]
        if len(same_dir) > max_zones:
            order_blocks.remove(same_dir[0])

    return inside_bull, inside_bear, active_bull_zone, active_bear_zone


# ==================== FAIR VALUE GAP ====================
def detect_fvg(candles):
    if len(candles) < 3:
        return None
    c0, c1, c2 = candles[-3], candles[-2], candles[-1]
    if c2["low"] > c0["high"] and c1["close"] > c1["open"]:
        return "bull"
    if c2["high"] < c0["low"] and c1["close"] < c1["open"]:
        return "bear"
    return None


# ==================== LIQUIDITY SWEEP ====================
def detect_liquidity_sweep(candles, lookback=10):
    if len(candles) < lookback + 1:
        return None
    c = candles[-1]
    recent = candles[-(lookback + 1):-1]
    recent_low = min(x["low"] for x in recent)
    recent_high = max(x["high"] for x in recent)
    if c["low"] < recent_low and c["close"] > c["low"] + (c["high"] - c["low"]) * 0.4:
        return "bull"
    if c["high"] > recent_high and c["close"] < c["high"] - (c["high"] - c["low"]) * 0.4:
        return "bear"
    return None


# ==================== DISPLACEMENT ====================
def detect_displacement(candles, atr_val, mult=1.3):
    c = candles[-1]
    if (c["close"] - c["open"]) > atr_val * mult:
        return "bull"
    if (c["open"] - c["close"]) > atr_val * mult:
        return "bear"
    return None


# ==================== REJECTION ====================
def detect_rejection(candles, wick_ratio=2.0):
    c = candles[-1]
    body = abs(c["close"] - c["open"])
    up_wick = c["high"] - max(c["close"], c["open"])
    lo_wick = min(c["close"], c["open"]) - c["low"]
    if lo_wick > body * wick_ratio and lo_wick > up_wick:
        return "bull"
    if up_wick > body * wick_ratio and up_wick > lo_wick:
        return "bear"
    return None


# ==================== SUPPORT / RESISTANCE (configurable touch minimum) ====================
def find_support_resistance(candles, lookback=2, touch_tolerance_atr_mult=0.25,
                             min_touches=3, atr_val=None):
    if atr_val is None:
        atr_val = atr(candles, 14)
    tolerance = atr_val * touch_tolerance_atr_mult

    points = swing_points(candles, lookback)
    levels = []

    for _, price, kind in points:
        level_type = "support" if kind == "low" else "resistance"
        matched = None
        for lvl in levels:
            if lvl["type"] == level_type and abs(lvl["price"] - price) <= tolerance:
                matched = lvl
                break
        if matched:
            matched["touches"].append(price)
            matched["price"] = sum(matched["touches"]) / len(matched["touches"])
        else:
            levels.append({"price": price, "touches": [price], "type": level_type})

    return [
        {"price": lvl["price"], "touches": len(lvl["touches"]), "type": lvl["type"]}
        for lvl in levels if len(lvl["touches"]) >= min_touches
    ]


def near_sr_level(current_price, sr_levels, atr_val, proximity_mult=0.3):
    tolerance = atr_val * proximity_mult
    candidates = [lvl for lvl in sr_levels if abs(lvl["price"] - current_price) <= tolerance]
    if not candidates:
        return None
    return min(candidates, key=lambda l: abs(l["price"] - current_price))


# ==================== SUPPLY / DEMAND ZONES ====================
def find_supply_demand_zones(candles, consolidation_bars=3, move_atr_mult=1.5, lookback_window=60):
    zones = []
    atr_val = atr(candles, 14)
    start = max(1, len(candles) - lookback_window)

    for i in range(start, len(candles) - consolidation_bars - 1):
        window = candles[i:i + consolidation_bars]
        w_high = max(c["high"] for c in window)
        w_low = min(c["low"] for c in window)
        if (w_high - w_low) > atr_val * 0.8:
            continue
        move_candle = candles[i + consolidation_bars]
        move_size = move_candle["close"] - move_candle["open"]
        if move_size > atr_val * move_atr_mult:
            zones.append({"type": "demand", "top": w_high, "bot": w_low, "index": i, "active": True})
        elif -move_size > atr_val * move_atr_mult:
            zones.append({"type": "supply", "top": w_high, "bot": w_low, "index": i, "active": True})

    return zones


def price_in_zone(current_price, zones, zone_type):
    for z in zones:
        if z["active"] and z["type"] == zone_type and z["bot"] <= current_price <= z["top"]:
            return z
    return None


# ==================== CONDITION LABEL + DURATION CLASSIFICATION ====================
def build_condition_label(zone, confirmations, direction, sr_hit, sd_hit):
    """
    Builds a label like 'OB+CHoCH+FVG+LIQ' from everything that actually
    fired for this signal, in a fixed, readable order.
    """
    parts = ["OB"]
    parts.append("CHoCH" if zone.get("is_choch") else "BOS")
    if confirmations.get("fvg") == direction:
        parts.append("FVG")
    if confirmations.get("liquidity") == direction:
        parts.append("LIQ")
    if confirmations.get("displacement") == direction:
        parts.append("DISP")
    if confirmations.get("rejection") == direction:
        parts.append("REJ")
    if sr_hit:
        parts.append("SR3")
    if sd_hit:
        parts.append("SD")
    return "+".join(parts)


def classify_trade_duration(zone, confirmations, direction, sr_hit, sd_hit):
    """
    Conviction score -> day trade vs swing hold recommendation.
    CHoCH (fresh trend reversal) and displacement carry the most weight
    since they indicate a genuinely new directional push, not just a
    pullback within an existing range. More confluence overall ->
    higher conviction -> worth holding beyond a single session.
    """
    score = 0
    if zone.get("is_choch"):
        score += 2
    if confirmations.get("displacement") == direction:
        score += 2
    if confirmations.get("fvg") == direction:
        score += 1
    if confirmations.get("liquidity") == direction:
        score += 1
    if confirmations.get("rejection") == direction:
        score += 1
    if sr_hit:
        score += 1
    if sd_hit:
        score += 1

    if score >= 5:
        return "SWING", "High conviction (CHoCH/displacement + multiple confluences) — manage by structure, no fixed time stop."
    elif score >= 3:
        return "DAY/SWING", "Moderate conviction — take partial at 1-2R, trail the rest if structure keeps supporting the move."
    else:
        return "DAY", "Lower conviction, single-confirmation setup — treat as intraday, take profit by 1-2R and close by session end."


# ==================== MAIN SWING SIGNAL FUNCTION ====================
def evaluate_swing_signal(trend_candles, structure_candles, entry_candles,
                           pair_state, swing_lookback=2, ob_max_zones=5,
                           liquidity_lookback=10, displacement_atr_mult=1.3,
                           rejection_wick_ratio=2.0, sl_buffer_atr_mult=0.15,
                           sr_min_touches=3):
    pair_state.setdefault("trend", "none")
    pair_state.setdefault("order_blocks", [])

    htf_closes = [c["close"] for c in trend_candles]
    htf_bias = "bull" if htf_closes[-1] > (sum(htf_closes[-50:]) / min(50, len(htf_closes))) else "bear"

    new_trend, brk = detect_structure_break(structure_candles, swing_lookback, pair_state["trend"])
    pair_state["trend"] = new_trend
    if brk:
        ob = build_order_block(structure_candles, brk["bos_index"], brk["direction"], brk["is_choch"])
        pair_state["order_blocks"].append(ob)

    atr_val = atr(structure_candles, 14)
    current_price = entry_candles[-1]["close"]

    inside_bull, inside_bear, bull_zone, bear_zone = update_order_blocks(
        pair_state["order_blocks"], current_price, max_zones=ob_max_zones
    )

    fvg = detect_fvg(entry_candles)
    liq = detect_liquidity_sweep(entry_candles, liquidity_lookback)
    disp = detect_displacement(entry_candles, atr_val, displacement_atr_mult)
    rej = detect_rejection(entry_candles, rejection_wick_ratio)
    confirmations = {"fvg": fvg, "liquidity": liq, "displacement": disp, "rejection": rej}

    def has_confirmation(direction):
        return any(v == direction for v in confirmations.values())

    sr_levels = find_support_resistance(structure_candles, swing_lookback, atr_val=atr_val, min_touches=sr_min_touches)
    near_level = near_sr_level(current_price, sr_levels, atr_val)
    sd_zones = find_supply_demand_zones(structure_candles)

    signal = None
    if inside_bull and htf_bias == "bull" and has_confirmation("bull"):
        sd_hit = price_in_zone(current_price, sd_zones, "demand")
        sl = bull_zone["bot"] - atr_val * sl_buffer_atr_mult
        label = build_condition_label(bull_zone, confirmations, "bull", near_level is not None, sd_hit is not None)
        duration, duration_note = classify_trade_duration(bull_zone, confirmations, "bull", near_level is not None, sd_hit is not None)
        signal = {"direction": "bull", "entry": current_price, "sl": sl, "zone": bull_zone,
                  "confirmations": confirmations, "htf_bias": htf_bias, "condition_label": label,
                  "sr_confluence": near_level, "sd_confluence": sd_hit,
                  "duration": duration, "duration_note": duration_note}

    elif inside_bear and htf_bias == "bear" and has_confirmation("bear"):
        sd_hit = price_in_zone(current_price, sd_zones, "supply")
        sl = bear_zone["top"] + atr_val * sl_buffer_atr_mult
        label = build_condition_label(bear_zone, confirmations, "bear", near_level is not None, sd_hit is not None)
        duration, duration_note = classify_trade_duration(bear_zone, confirmations, "bear", near_level is not None, sd_hit is not None)
        signal = {"direction": "bear", "entry": current_price, "sl": sl, "zone": bear_zone,
                  "confirmations": confirmations, "htf_bias": htf_bias, "condition_label": label,
                  "sr_confluence": near_level, "sd_confluence": sd_hit,
                  "duration": duration, "duration_note": duration_note}

    return signal
