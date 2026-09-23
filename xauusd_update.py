import os
import requests
import yfinance as yf
import mplfinance as mpf
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# ================== LIVE DATA ==================
def get_live_data(symbol="GC=F", interval="30m", period="5d"):
    ticker = yf.Ticker(symbol)
    df = ticker.history(period=period, interval=interval)
    if df.empty:
        raise ValueError("No data received")
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']]
    return df, round(df['Close'].iloc[-1], 2)


# ================== SWING DETECTION ==================
def detect_swings(df, left=3, right=3):
    df = df.copy()
    df['Swing_High'] = np.nan
    df['Swing_Low'] = np.nan

    for i in range(left, len(df) - right):
        if df['High'].iloc[i] == df['High'].iloc[i-left:i+right+1].max():
            df.loc[df.index[i], 'Swing_High'] = df['High'].iloc[i]
        if df['Low'].iloc[i] == df['Low'].iloc[i-left:i+right+1].min():
            df.loc[df.index[i], 'Swing_Low'] = df['Low'].iloc[i]
    return df


def analyze_structure(df):
    swings_high = df[df['Swing_High'].notna()][['Swing_High']].tail(4)
    swings_low = df[df['Swing_Low'].notna()][['Swing_Low']].tail(4)

    structure_notes = []
    bias = "Neutral"

    if len(swings_high) >= 2:
        if swings_high['Swing_High'].iloc[-1] < swings_high['Swing_High'].iloc[-2]:
            structure_notes.append("Lower High confirmed")
            bias = "Bearish"
        else:
            structure_notes.append("Higher High")

    if len(swings_low) >= 2:
        if swings_low['Swing_Low'].iloc[-1] > swings_low['Swing_Low'].iloc[-2]:
            structure_notes.append("Higher Low (High Low)")
        else:
            structure_notes.append("Lower Low")
            bias = "Bearish"

    return structure_notes, bias, swings_high, swings_low


# ================== CHART ==================
def create_rajabanks_chart(df, filename="raja_chart.png"):
    df = detect_swings(df)
    structure_notes, bias, swings_high, swings_low = analyze_structure(df)

    mc = mpf.make_marketcolors(up='#26a69a', down='#ef5350', edge='inherit', wick='inherit')
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=':',
        gridcolor='#b0bec5',
        facecolor='#e3f2fd',
        figcolor='#e3f2fd',
        y_on_right=True
    )

    fig, axes = mpf.plot(
        df.tail(80),
        type='candle',
        style=style,
        title=f"XAUUSD M30 | Structure ({bias})",
        ylabel='Price',
        volume=False,
        figsize=(13, 7),
        returnfig=True
    )

    ax = axes[0]

    for idx, row in swings_high.tail(2).iterrows():
        ax.annotate("Lower High" if bias == "Bearish" else "Swing High",
                    xy=(idx, row['Swing_High']),
                    xytext=(0, 12), textcoords='offset points',
                    ha='center', color='red', fontsize=8, fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='red'))

    for idx, row in swings_low.tail(2).iterrows():
        ax.annotate("High Low",
                    xy=(idx, row['Swing_Low']),
                    xytext=(0, -15), textcoords='offset points',
                    ha='center', color='green', fontsize=8, fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color='green'))

    ax.annotate("Potential Liquidity Re-Sweep Zone",
                xy=(0.68, 0.08), xycoords='axes fraction',
                color='red', fontsize=8, fontweight='bold',
                bbox=dict(boxstyle="round,pad=0.3", facecolor='white', edgecolor='red', alpha=0.9))

    plt.savefig(filename, dpi=160, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    return filename, structure_notes, bias


# ================== MESSAGE ==================
def create_rajabanks_message(price, structure_notes, bias):
    date_str = datetime.now().strftime("%B %d").upper()
    notes_text = "\n".join([f"• {note}" for note in structure_notes]) if structure_notes else "• Structure developing"

    message = f"""RAJA banks set-up — XAUUSD/M30 — Intraday...

📌 <b>MARKET UPDATE – {date_str}</b>

<b>RAJA Gold</b>
— XAUUSD / M30 —

🔥 <b>TRADING PLAN</b>

XAUUSD is trading around <b>{price:,.2f}</b>.

<b>Structure:</b>
{notes_text}

Price is approaching a potential liquidity zone.  
If a liquidity sweep occurs with strong rejection, we can look for a recovery toward the next price range.

<b>Bias:</b> {bias} — Wait for liquidity sweep + clear reaction.
"""
    return message


# ================== SEND ==================
def send_telegram_photo(photo_path, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as photo:
        files = {"photo": photo}
        data = {
            "chat_id": CHAT_ID,
            "caption": caption,
            "parse_mode": "HTML"
        }
        response = requests.post(url, files=files, data=data)
    return response.json()


# ================== MAIN ==================
if __name__ == "__main__":
    try:
        df, price = get_live_data(interval="30m", period="5d")
        chart_file, notes, bias = create_rajabanks_chart(df)
        caption = create_rajabanks_message(price, notes, bias)
        result = send_telegram_photo(chart_file, caption)

        if result.get("ok"):
            print("Update sent successfully")
        else:
            print("Error:", result)

        if os.path.exists(chart_file):
            os.remove(chart_file)

    except Exception as e:
        print("Error:", str(e))
