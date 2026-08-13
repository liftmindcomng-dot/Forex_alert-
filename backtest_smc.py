"""
Backtests the SMC/ICT signal logic in forex_alert.py (sweep + MSS/CHoCH
+ required FVG or Order Block, on 5-minute candles) against a deep
window of real historical data from Twelve Data.

Unlike a single time_series call (capped around 5,000 bars), this pages
backward through multiple chunks using Twelve Data's `end_date` param,
so you can backtest months of 5-minute data instead of just the last
~17 days.

Reuses the SAME compute_signal() function your live bot calls, imported
directly from forex_alert_smc.py — so the backtest and that strategy
file can never quietly drift out of sync with each other.

SETUP:
    export TWELVE_DATA_API_KEY=your_real_key
    export TELEGRAM_BOT_TOKEN=dummy
    export TELEGRAM_CHAT_ID=dummy

USAGE:
    python backtest_smc.py --pair XAU/USD --interval 5min --chunks 6

  --chunks controls how far back you go: each chunk pulls up to 5,000
  bars, then the next chunk continues from where that one left off.
  6 chunks of 5-minute bars is roughly 100+ trading days, depending on
  market hours for the pair.
"""

import argparse
import json
import time
import urllib.request
import urllib.parse

from forex_alert_smc import API_KEY, compute_signal, RR_RATIO, send_telegram

CHUNK_SIZE = 5000


def fetch_chunk(pair, interval, outputsize, end_date=None):
    params = {"symbol": pair, "interval": interval, "outputsize": outputsize, "apikey": API_KEY}
    if end_date:
        params["end_date"] = end_date
    url = "https://api.twelvedata.com/time_series?
