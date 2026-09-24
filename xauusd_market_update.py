"""
Standalone XAUUSD Market Update poster.

Separate from forex_alert.py by design — this is a simpler, single-purpose
script that posts a narrative structure chart on a plain schedule, with no
state file and no "only on fresh break" gating (see the workflow's cron:
it fires every run, on purpose, for a steady drumbeat of updates rather
than a rarer event-driven post).

Feed: Twelve Data's XAU/USD SPOT feed (TWELVE_DATA_API_KEY), matching
forex_alert.py's price source exactly.

Structure detection: real BOS vs CHoCH distinction (a break is CHoCH if
it reverses the prevailing multi-swing trend, BOS if it continues it),
order block detection (last opposite-colored candle before the
impulsive breakout leg, now supporting multiple stacked zones), and a
displacement filter (breaking candle's range must clear a minimum ATR
multiple).

Bias now requires the last-two-swing-highs AND last-two-swing-lows to
actually agree on direction before calling a trend (Bullish/Bearish).
When they disagree (a contracting or expanding range), bias is Neutral
and the post describes the range honestly instead of forcing a
directional narrative onto data that doesn't support one.

Chart styling: bold arrow markers at swing points (labeled by each
swing's own direction), an order-block zone on a real trend or a
range box on Neutral, a current-price badge, and a computed
(ATR-based, not hand-drawn) projection line toward a "Price range"
target — only drawn when there's an actual directional bias.
"""

import os
import json
import urllib.request
import urllib.parse
import pandas as pd
import numpy as np
import mplfinance as mpf
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import datetime

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")

PAIR = os.getenv("XAU_PAIR", "XAU/USD")
INTERVAL = os.getenv("XAU_INTERVAL", "30min")   # Twelve Data format, e.g. "30min", "1h", "15min"
OUTPUT_SIZE = int(os.getenv("XAU_OUTPUTSIZE", "150"))

SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "3"))       # bars each side for a confirmed pivot
TREND_LOOKBACK_SWINGS = int(os.getenv("TREND_LOOKBACK_SWINGS", "4"))  # how many swings back to infer prior trend
OB_LOOKBACK = int(os.getenv("OB_LOOKBACK", "15"))
DISPLACEMENT_ATR_MULT = float(os.getenv("DISPLACEMENT_ATR_MULT", "1.0"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))
PROJECTION_ATR_MULT = float(os.getenv("PROJECTION_ATR_MULT", "2.0"))


# ================== LIVE DATA (Twelve Data, spot XAU/USD) ==================
def get_live_data(pair=PAIR, interval=INTERVAL, outputsize=OUTPUT_SIZE):
    """Fetch spot XAU/USD from Twelve Data — same feed forex_alert.py
    uses, so this poster's price/structure reads match the main bot
    instead of drifting against a futures feed."""
    if not TWELVE_DATA_API_KEY:
        raise RuntimeError("TWELVE_DATA_API_KEY is not set.")

    url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode({
        "symbol": pair,
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
        "timezone": "UTC",
    })
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode())
    if "values" not in data:
        raise RuntimeError(f"Twelve Data error [{pair} {interval}]: {data.get('message', data)}")

    rows = list(reversed(data["values"]))  # oldest -> newest
    df = pd.DataFrame({
        "Open": [float(r["open"]) for r in rows],
        "High": [float(r["high"]) for r in rows],
        "Low": [float(r["low"]) for r in rows],
        "Close": [float(r["close"]) for r in rows],
    }, index=pd.to_datetime([r["datetime"] for r in rows]))

    if df.empty:
        raise ValueError("No data received")
    return df, round(df["Close"].iloc[-1], 2)


# ================== INDICATORS ==================
def atr(df, period=ATR_PERIOD):
    highs, lows, closes = df["High"].values, df["Low"].values, df["Close"].values
    n = len(closes)
    if n <= period:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    avg = sum(trs[:period]) / period
    for tr in trs[period:]:
        avg = (avg * (period - 1) + tr) / period
    return avg


# ================== SWING DETECTION ==================
def detect_swings(df, left=SWING_LOOKBACK, right=SWING_LOOKBACK):
    df = df.copy()
    df["Swing_High"] = np.nan
    df["Swing_Low"] = np.nan

    for i in range(left, len(df) - right):
        if df["High"].iloc[i] == df["High"].iloc[i - left:i + right + 1].max():
            df.loc[df.index[i], "Swing_High"] = df["High"].iloc[i]
        if df["Low"].iloc[i] == df["Low"].iloc[i - left:i + right + 1].min():
            df.loc[df.index[i], "Swing_Low"] = df["Low"].iloc[i]
    return df


def find_order_block(df, bias, before_idx, lookback=OB_LOOKBACK):
    """Last opposite-colored candle before the impulsive breakout leg —
    same definition forex_alert.py uses. Returns {"high","low"} or None."""
    opens, closes = df["Open"].values, df["Close"].values
    highs, lows = df["High"].values, df["Low"].values
    start = max(0, before_idx - lookback)
    for i in range(before_idx - 1, start - 1, -1):
        is_bearish = closes[i] < opens[i]
        is_bullish = closes[i] > opens[i]
        if (bias == "Bullish" and is_bearish) or (bias == "Bearish" and is_bullish):
            return {"high": highs[i], "low": lows[i]}
    return None


def find_order_blocks_multi(df, bias, swings, lookback=OB_LOOKBACK, max_zones=3):
    """Walks the last few same-direction swings and finds the order block
    preceding each one, so the chart shows a history of zones (like an
    analyst marking several boxes along the move) instead of just the
    single most recent one."""
    zones = []
    for idx in swings.tail(max_zones).index:
        before_idx = df.index.get_loc(idx)
        ob = find_order_block(df, bias, before_idx=before_idx, lookback=lookback)
        if ob and ob not in zones:
            zones.append(ob)
    return zones


def analyze_structure_deep(df):
    """Deeper structure read than the original 2-swing version:
      - Bias now requires the last-two-swing-highs AND last-two-swing-lows
        to actually agree on direction (both lower = Bearish, both higher
        = Bullish). Previously the high check and low check ran
        independently and the low check's result silently overwrote the
        high check's — so a Lower High + Higher Low (a contracting range,
        not a trend) could still get tagged "Bearish" from the high alone,
        and the narrative would print both notes side by side as if they
        told one consistent story. Disagreement is now reported honestly
        as a range (Neutral bias, no break/order-block computed) instead
        of a fabricated trend call.
      - BOS vs CHoCH: compares the latest swing break against the trend
        implied by the TREND_LOOKBACK_SWINGS swings before it, so a
        break that reverses the prior trend is tagged CHoCH, one that
        continues it is tagged BOS. (No persisted state file here, so
        "prior trend" is inferred from a longer lookback within the
        same fetch rather than carried over from a previous run.)
      - Displacement filter: the breaking candle's range must be at
        least DISPLACEMENT_ATR_MULT x ATR, or the break is not reported.
      - Order block: the zone the move likely originated from, drawn at
        its real computed price level rather than a fixed label position.

    Returns (structure_notes, bias, break_kind, break_index, break_level,
    swings_high, swings_low, order_block, displacement_hit, atr_val).
    """
    df = detect_swings(df)
    swings_high_all = df[df["Swing_High"].notna()][["Swing_High"]]
    swings_low_all = df[df["Swing_Low"].notna()][["Swing_Low"]]
    atr_val = atr(df)

    swings_high = swings_high_all.tail(4)
    swings_low = swings_low_all.tail(4)

    high_dir = None
    low_dir = None
    if len(swings_high) >= 2:
        high_dir = "lower" if swings_high["Swing_High"].iloc[-1] < swings_high["Swing_High"].iloc[-2] else "higher"
    if len(swings_low) >= 2:
        low_dir = "lower" if swings_low["Swing_Low"].iloc[-1] < swings_low["Swing_Low"].iloc[-2] else "higher"

    structure_notes = []
    bias = "Neutral"

    if high_dir and low_dir:
        if high_dir == "lower" and low_dir == "lower":
            bias = "Bearish"
            structure_notes.append("Lower High confirmed")
            structure_notes.append("Lower Low")
        elif high_dir == "higher" and low_dir == "higher":
            bias = "Bullish"
            structure_notes.append("Higher High")
            structure_notes.append("Higher Low")
        elif high_dir == "lower" and low_dir == "higher":
            bias = "Neutral"
            structure_notes.append("Lower High confirmed")
            structure_notes.append("Higher Low")
            structure_notes.append("Contracting range — no clear trend")
        else:  # higher high, lower low
            bias = "Neutral"
            structure_notes.append("Higher High")
            structure_notes.append("Lower Low")
            structure_notes.append("Expanding range — no clear trend")
    elif high_dir:
        bias = "Bearish" if high_dir == "lower" else "Bullish"
        structure_notes.append("Lower High confirmed" if high_dir == "lower" else "Higher High")
    elif low_dir:
        bias = "Bullish" if low_dir == "higher" else "Bearish"
        structure_notes.append("Higher Low" if low_dir == "higher" else "Lower Low")

    # --- infer the PRIOR trend from a longer swing lookback, to decide
    # BOS vs CHoCH for the most recent break ---
    prior_bias = "Neutral"
    hh_prior = swings_high_all.tail(TREND_LOOKBACK_SWINGS + 1)
    ll_prior = swings_low_all.tail(TREND_LOOKBACK_SWINGS + 1)
    if len(hh_prior) >= TREND_LOOKBACK_SWINGS and len(ll_prior) >= TREND_LOOKBACK_SWINGS:
        hh_excl_latest = hh_prior.iloc[:-1] if len(hh_prior) > TREND_LOOKBACK_SWINGS else hh_prior
        ll_excl_latest = ll_prior.iloc[:-1] if len(ll_prior) > TREND_LOOKBACK_SWINGS else ll_prior
        if len(hh_excl_latest) >= 2 and hh_excl_latest["Swing_High"].iloc[-1] > hh_excl_latest["Swing_High"].iloc[-2]:
            prior_bias = "Bullish"
        elif len(hh_excl_latest) >= 2:
            prior_bias = "Bearish"
        if prior_bias == "Neutral" and len(ll_excl_latest) >= 2:
            prior_bias = "Bullish" if ll_excl_latest["Swing_Low"].iloc[-1] > ll_excl_latest["Swing_Low"].iloc[-2] else "Bearish"

    break_kind = None
    break_index = None
    break_level = None
    order_block = None
    displacement_hit = False

    if bias in ("Bullish", "Bearish") and (len(swings_high) >= 1 or len(swings_low) >= 1):
        n = len(df)
        last_close = df["Close"].iloc[-1]
        if bias == "Bullish" and len(swings_high) >= 1:
            level_row = swings_high.iloc[-1]
            break_level = level_row["Swing_High"]
            break_index = df.index.get_loc(swings_high.index[-1])
        elif bias == "Bearish" and len(swings_low) >= 1:
            level_row = swings_low.iloc[-1]
            break_level = level_row["Swing_Low"]
            break_index = df.index.get_loc(swings_low.index[-1])

        if break_level is not None:
            broke = (last_close > break_level) if bias == "Bullish" else (last_close < break_level)
            if broke:
                last_range = df["High"].iloc[-1] - df["Low"].iloc[-1]
                displacement_hit = bool(atr_val) and last_range >= DISPLACEMENT_ATR_MULT * atr_val
                break_kind = "CHoCH" if bias != prior_bias and prior_bias != "Neutral" else "BOS"
                order_block = find_order_block(df, bias, before_idx=n - 1, lookback=OB_LOOKBACK)
                structure_notes.append(
                    f"{break_kind} confirmed" + (" with displacement" if displacement_hit else " (no displacement)")
                )
            else:
                break_level = None  # no actual break yet, just structure sequence

    return (structure_notes, bias, break_kind, break_index, break_level,
            swings_high, swings_low, order_block, displacement_hit, atr_val)


# ================== CHART ==================
def create_chart(df, filename="xauusd_chart.png"):
    (structure_notes, bias, break_kind, break_index, break_level,
     swings_high, swings_low, order_block, displacement_hit, atr_val) = analyze_structure_deep(df)

    mc = mpf.make_marketcolors(up="#26a69a", down="#ef5350", edge="inherit", wick="inherit")
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=":",
        gridcolor="#b0bec5",
        facecolor="#e3f2fd",
        figcolor="#e3f2fd",
        y_on_right=True,
    )

    plot_df = df.tail(80)
    fig, axes = mpf.plot(
        plot_df,
        type="candle",
        style=style,
        title=f"XAUUSD {INTERVAL.upper()} | Structure ({bias})",
        ylabel="Price",
        volume=False,
        figsize=(13, 7),
        returnfig=True,
    )
    ax = axes[0]
    offset_start = len(df) - len(plot_df)
    last_x = len(plot_df) - 1

    # --- swing markers as bold arrows (label each swing by its own
    # actual direction now, not by the overall bias — needed since bias
    # can be Neutral on a range, where highs and lows point opposite
    # ways and a single bias-based label would be wrong for one side) ---
    for idx, row in swings_high.tail(2).iterrows():
        pos = df.index.get_loc(idx) - offset_start
        if pos < 0:
            continue
        is_latest_lower = (
            len(swings_high) >= 2
            and swings_high["Swing_High"].iloc[-1] < swings_high["Swing_High"].iloc[-2]
        )
        label = "Lower High" if is_latest_lower else "Higher High"
        ax.annotate("", xy=(pos, row["Swing_High"]), xytext=(pos, row["Swing_High"] + (atr_val or 1) * 1.8),
                    arrowprops=dict(arrowstyle="-|>", color="#d32f2f", lw=2))
        ax.annotate(label, xy=(pos, row["Swing_High"] + (atr_val or 1) * 2.0),
                    ha="center", fontsize=8, fontweight="bold", color="#d32f2f")

    for idx, row in swings_low.tail(2).iterrows():
        pos = df.index.get_loc(idx) - offset_start
        if pos < 0:
            continue
        is_latest_higher = (
            len(swings_low) >= 2
            and swings_low["Swing_Low"].iloc[-1] > swings_low["Swing_Low"].iloc[-2]
        )
        label = "Higher Low" if is_latest_higher else "Lower Low"
        ax.annotate("", xy=(pos, row["Swing_Low"]), xytext=(pos, row["Swing_Low"] - (atr_val or 1) * 1.8),
                    arrowprops=dict(arrowstyle="-|>", color="#2e7d32", lw=2))
        ax.annotate(label, xy=(pos, row["Swing_Low"] - (atr_val or 1) * 2.0),
                    ha="center", fontsize=8, fontweight="bold", color="#2e7d32")

    # --- BOS/CHoCH line ---
    if break_kind and break_index is not None:
        bx = break_index - offset_start
        if bx >= 0:
            col = "#d500f9" if break_kind == "CHoCH" else "#111"
            ax.plot([bx, last_x], [break_level, break_level], linestyle="--", linewidth=1, color=col, alpha=0.8)
            ax.annotate(break_kind, xy=((bx + last_x) / 2, break_level), fontsize=9, fontweight="bold",
                        color=col, ha="center", va="bottom")

    # --- multiple order-block zones (only meaningful for a real
    # Bullish/Bearish bias — a Neutral/range read has no directional
    # break to draw a zone from) ---
    zones = []
    if bias in ("Bullish", "Bearish"):
        swings_for_ob = swings_low if bias == "Bullish" else swings_high
        zones = find_order_blocks_multi(df, bias, swings_for_ob, lookback=OB_LOOKBACK, max_zones=3)
        zone_color = "#26a69a" if bias == "Bullish" else "#ef5350"
        for i, ob in enumerate(zones):
            rect = plt.Rectangle((0, ob["low"]), last_x, ob["high"] - ob["low"],
                                  facecolor=zone_color + "22", edgecolor=zone_color, linewidth=0.8, zorder=1)
            ax.add_patch(rect)
            if i == 0:
                ax.annotate("Order Block / Re-Sweep Zone", xy=(last_x * 0.15, (ob["high"] + ob["low"]) / 2),
                            color=zone_color, fontsize=8, fontweight="bold",
                            bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor=zone_color, alpha=0.9))
    elif len(swings_high) >= 1 and len(swings_low) >= 1:
        # Range: shade the box between the most recent swing high and
        # swing low instead of a directional order block.
        range_hi = swings_high["Swing_High"].iloc[-1]
        range_lo = swings_low["Swing_Low"].iloc[-1]
        rect = plt.Rectangle((0, range_lo), last_x, range_hi - range_lo,
                              facecolor="#9e9e9e22", edgecolor="#616161", linewidth=0.8, zorder=1)
        ax.add_patch(rect)
        ax.annotate("Range — awaiting breakout", xy=(last_x * 0.15, (range_hi + range_lo) / 2),
                    color="#616161", fontsize=8, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white", edgecolor="#616161", alpha=0.9))

    # --- current price badge ---
    last_price = df["Close"].iloc[-1]
    badge_color = "#26a69a" if bias == "Bullish" else ("#ef5350" if bias == "Bearish" else "#616161")
    ax.annotate(f"{last_price:,.2f}", xy=(last_x, last_price), xytext=(12, 0), textcoords="offset points",
                fontsize=10, fontweight="bold", color="white", va="center",
                bbox=dict(boxstyle="round,pad=0.4", facecolor=badge_color, edgecolor="none"))

    # --- ATR-based projected target (only meaningful with a directional
    # bias — a range has no "direction" to project toward) ---
    target = None
    if atr_val and bias in ("Bullish", "Bearish"):
        direction = 1 if bias == "Bullish" else -1
        target = last_price + direction * PROJECTION_ATR_MULT * atr_val
        mid_x = last_x + (len(plot_df) * 0.15)
        end_x = last_x + (len(plot_df) * 0.3)
        pullback = last_price - direction * atr_val * 0.5
        ax.plot([last_x, mid_x, end_x], [last_price, pullback, target],
                linestyle="--", linewidth=1.2, color="#616161", alpha=0.8)
        ax.annotate("Price range", xy=(end_x, target), xytext=(6, 0), textcoords="offset points",
                    fontsize=8, fontweight="bold", color="#424242")
        ax.set_xlim(right=end_x + 3)

    plt.savefig(filename, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return filename, structure_notes, bias, break_kind, displacement_hit, order_block, target


# ================== MESSAGE ==================
def create_message(price, structure_notes, bias, break_kind, displacement_hit, order_block, target,
                    swings_high=None, swings_low=None):
    date_str = datetime.now().strftime("%B %d").upper()

    if bias == "Neutral":
        # A genuine contracting/expanding range: highs and lows disagree,
        # so there's no trend to call. Say what the range actually is and
        # what would need to happen to break it, instead of forcing a
        # directional narrative onto data that doesn't support one.
        range_hi = swings_high["Swing_High"].iloc[-1] if swings_high is not None and len(swings_high) >= 1 else None
        range_lo = swings_low["Swing_Low"].iloc[-1] if swings_low is not None and len(swings_low) >= 1 else None

        notes_text = "\n".join([f"• {note}" for note in structure_notes]) if structure_notes else "• Structure developing"

        if range_hi is not None and range_lo is not None:
            range_line = (
                f"Price is consolidating between <b>{range_lo:,.2f}</b> and <b>{range_hi:,.2f}</b> — "
                "no clear directional break yet.\n"
            )
            watch_line = (
                f"\nWatch for a confirmed break above <b>{range_hi:,.2f}</b> (bullish) "
                f"or below <b>{range_lo:,.2f}</b> (bearish) to establish the next trend.\n"
            )
        else:
            range_line = "Structure is still forming — not enough confirmed swings yet for a range read.\n"
            watch_line = ""

        return f"""XAUUSD {INTERVAL.upper()} Setup — Intraday

📌 <b>MARKET UPDATE – {date_str}</b>

— {PAIR.replace('/', '')} / {INTERVAL.upper()} —

🔥 <b>TRADING PLAN</b>

XAUUSD is trading around <b>{price:,.2f}</b>.

<b>Structure:</b>
{notes_text}

{range_line}{watch_line}
<b>Bias:</b> Neutral — no trade bias until price breaks the range.
"""

    notes_text = "\n".join([f"• {note}" for note in structure_notes]) if structure_notes else "• Structure developing"

    break_line = ""
    if break_kind:
        disp_word = "with clear displacement" if displacement_hit else "without strong displacement"
        break_line = f"\n<b>{break_kind}</b> confirmed {disp_word} on {PAIR.replace('/', '')} {INTERVAL.upper()}.\n"

    if order_block:
        zone_line = (
            "Price is approaching a live order-block / re-sweep zone "
            f"({order_block['low']:,.2f} – {order_block['high']:,.2f}).\n"
        )
    else:
        zone_line = "No active order-block zone currently in play — structure is still developing.\n"

    target_line = f"\nProjected range target (ATR-based): <b>{target:,.2f}</b>\n" if target else ""

    message = f"""XAUUSD {INTERVAL.upper()} Setup — Intraday

📌 <b>MARKET UPDATE – {date_str}</b>

— {PAIR.replace('/', '')} / {INTERVAL.upper()} —

🔥 <b>TRADING PLAN</b>

XAUUSD is trading around <b>{price:,.2f}</b>.
{break_line}
<b>Structure:</b>
{notes_text}

{zone_line}{target_line}
<b>Bias:</b> {bias} — Wait for reaction at the zone.
"""
    return message


# ================== SEND ==================
def send_telegram_photo(photo_path, caption):
    import requests
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as photo:
        files = {"photo": photo}
        data = {
            "chat_id": CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML",
        }
        response = requests.post(url, files=files, data=data)
    return response.json()


# ================== MAIN ==================
if __name__ == "__main__":
    try:
        df, price = get_live_data()
        chart_file, notes, bias, break_kind, displacement_hit, order_block, target = create_chart(df)
        # analyze_structure_deep also gives us swings_high/swings_low —
        # recompute once here (cheap) so create_message can build the
        # range line on a Neutral read.
        (_, _, _, _, _, swings_high, swings_low, _, _, _) = analyze_structure_deep(df)
        caption = create_message(price, notes, bias, break_kind, displacement_hit, order_block, target,
                                  swings_high=swings_high, swings_low=swings_low)
        result = send_telegram_photo(chart_file, caption)

        if result.get("ok"):
            print("Update sent successfully")
        else:
            print("Error:", result)

        if os.path.exists(chart_file):
            os.remove(chart_file)

    except Exception as e:
        print("Error:", str(e))
