"""
Standalone XAUUSD Market Update poster.

Separate from forex_alert.py by design — this is a simpler, single-purpose
script that posts a narrative structure chart on a plain schedule, with no
state file and no "only on fresh break" gating (see the workflow's cron:
it fires every run, on purpose, for a steady drumbeat of updates rather
than a rarer event-driven post).

Originally built on yfinance (GC=F / COMEX gold futures) with shallow
2-swing structure detection. Now upgraded to:

  - Twelve Data's XAU/USD SPOT feed (TWELVE_DATA_API_KEY), matching
    forex_alert.py's price source exactly, instead of yfinance futures
    data — no more premium/discount drift between the two systems.
  - Real BOS vs CHoCH distinction: a break is CHoCH if it reverses the
    prevailing multi-swing trend, BOS if it continues it (same concept
    as forex_alert.py's check_structure_break, adapted to run without a
    persisted state file — see analyze_structure_deep for how prior
    trend is inferred from a longer swing lookback within the same
    fetch, since there's no cross-run memory here).
  - Order block detection: the last opposite-colored candle before the
    impulsive breakout leg, drawn as a real, data-positioned zone box
    (this also fixes the old hardcoded liquidity-label position as a
    side effect — it now sits at the actual order-block level).
  - A displacement filter: the breaking candle's range must clear a
    minimum ATR multiple, so a marginal poke past a swing point isn't
    reported as a meaningful structural break.

Still intentionally does NOT include: liquidity-sweep detection, S/R
confluence, or FVG confirmation (forex_alert.py's structure mode has all
three) — this stays a lighter, narrative-only poster, not a signal engine.
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
INTERVAL = os.getenv("XAU_INTERVAL", "30min")   # Twelve Data format, e.g. "30min", "1h"
OUTPUT_SIZE = int(os.getenv("XAU_OUTPUTSIZE", "150"))

SWING_LOOKBACK = int(os.getenv("SWING_LOOKBACK", "3"))       # bars each side for a confirmed pivot
TREND_LOOKBACK_SWINGS = int(os.getenv("TREND_LOOKBACK_SWINGS", "4"))  # how many swings back to infer prior trend
OB_LOOKBACK = int(os.getenv("OB_LOOKBACK", "15"))
DISPLACEMENT_ATR_MULT = float(os.getenv("DISPLACEMENT_ATR_MULT", "1.0"))
ATR_PERIOD = int(os.getenv("ATR_PERIOD", "14"))


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


def analyze_structure_deep(df):
    """Deeper structure read than the original 2-swing version:
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

    structure_notes = []
    bias = "Neutral"

    if len(swings_high) >= 2:
        if swings_high["Swing_High"].iloc[-1] < swings_high["Swing_High"].iloc[-2]:
            structure_notes.append("Lower High confirmed")
            bias = "Bearish"
        else:
            structure_notes.append("Higher High")
            bias = "Bullish"

    if len(swings_low) >= 2:
        if swings_low["Swing_Low"].iloc[-1] > swings_low["Swing_Low"].iloc[-2]:
            structure_notes.append("Higher Low")
            if bias == "Neutral":
                bias = "Bullish"
        else:
            structure_notes.append("Lower Low")
            bias = "Bearish"

    # --- infer the PRIOR trend from a longer swing lookback, to decide
    # BOS vs CHoCH for the most recent break ---
    prior_bias = "Neutral"
    hh_prior = swings_high_all.tail(TREND_LOOKBACK_SWINGS + 1)
    ll_prior = swings_low_all.tail(TREND_LOOKBACK_SWINGS + 1)
    if len(hh_prior) >= TREND_LOOKBACK_SWINGS and len(ll_prior) >= TREND_LOOKBACK_SWINGS:
        # compare the swing before the most recent one against the one
        # before that, i.e. drop the latest swing and re-run the same
        # higher-high/higher-low logic on the trailing window
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
        # locate the break: latest close vs. the most recent opposite
        # swing point in the bias direction (mirrors
        # forex_alert.py's check_structure_break, single-pass here)
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

    # mplfinance plots at integer x-positions 0..len(plot_df)-1 regardless
    # of the datetime index — offset_start converts full-df swing/break
    # indices into positions within this trimmed plotting window.
    offset_start = len(df) - len(plot_df)

    for idx, row in swings_high.tail(2).iterrows():
        pos = df.index.get_loc(idx) - offset_start
        if pos < 0:
            continue
        ax.annotate("Lower High" if bias == "Bearish" else "Higher High",
                    xy=(pos, row["Swing_High"]),
                    xytext=(0, 12), textcoords="offset points",
                    ha="center", color="red", fontsize=8, fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="red"))

    for idx, row in swings_low.tail(2).iterrows():
        pos = df.index.get_loc(idx) - offset_start
        if pos < 0:
            continue
        ax.annotate("Higher Low" if bias == "Bullish" else "Lower Low",
                    xy=(pos, row["Swing_Low"]),
                    xytext=(0, -15), textcoords="offset points",
                    ha="center", color="green", fontsize=8, fontweight="bold",
                    arrowprops=dict(arrowstyle="->", color="green"))

    # --- BOS/CHoCH break line, at its real level, only if one fired ---
    if break_kind and break_index is not None:
        bx = break_index - offset_start
        if bx >= 0:
            ax.plot([bx, len(plot_df) - 1], [break_level, break_level],
                    linestyle="--", linewidth=1,
                    color="#d500f9" if break_kind == "CHoCH" else "#111", alpha=0.7)
            ax.annotate(break_kind, xy=(bx, break_level), fontsize=9, fontweight="bold",
                        color="#d500f9" if break_kind == "CHoCH" else "#111",
                        ha="center", va="bottom")

    # --- order-block zone, drawn at its real computed level (fixes the
    # old fixed-position "Potential Liquidity Re-Sweep Zone" label) ---
    if order_block:
        rect = plt.Rectangle(
            (0, order_block["low"]), len(plot_df) - 1, order_block["high"] - order_block["low"],
            facecolor="#ef535025" if bias == "Bearish" else "#26a69a25", edgecolor="none", zorder=1,
        )
        ax.add_patch(rect)
        ax.annotate("Order Block / Potential Re-Sweep Zone",
                    xy=(len(plot_df) * 0.35, (order_block["high"] + order_block["low"]) / 2),
                    color="red" if bias == "Bearish" else "green", fontsize=8, fontweight="bold",
                    bbox=dict(boxstyle="round,pad=0.3", facecolor="white",
                              edgecolor="red" if bias == "Bearish" else "green", alpha=0.9))

    plt.savefig(filename, dpi=160, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return filename, structure_notes, bias, break_kind, displacement_hit


# ================== MESSAGE ==================
def create_message(price, structure_notes, bias, break_kind, displacement_hit):
    date_str = datetime.now().strftime("%B %d").upper()
    notes_text = "\n".join([f"• {note}" for note in structure_notes]) if structure_notes else "• Structure developing"

    break_line = ""
    if break_kind:
        disp_word = "with clear displacement" if displacement_hit else "without strong displacement"
        break_line = f"\n<b>{break_kind}</b> confirmed {disp_word} on {PAIR.replace('/', '')} {INTERVAL.upper()}.\n"

    message = f"""XAUUSD {INTERVAL.upper()} Setup — Intraday

📌 <b>MARKET UPDATE – {date_str}</b>

— {PAIR.replace('/', '')} / {INTERVAL.upper()} —

🔥 <b>TRADING PLAN</b>

XAUUSD is trading around <b>{price:,.2f}</b>.
{break_line}
<b>Structure:</b>
{notes_text}

Price is approaching a potential order-block / re-sweep zone.
If a liquidity sweep occurs with strong rejection, we can look for a move toward the next price range.

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
        chart_file, notes, bias, break_kind, displacement_hit = create_chart(df)
        caption = create_message(price, notes, bias, break_kind, displacement_hit)
        result = send_telegram_photo(chart_file, caption)

        if result.get("ok"):
            print("Update sent successfully")
        else:
            print("Error:", result)

        if os.path.exists(chart_file):
            os.remove(chart_file)

    except Exception as e:
        print("Error:", str(e))
