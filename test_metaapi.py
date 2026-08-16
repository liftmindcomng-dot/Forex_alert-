"""
One-off test: places a single small market BUY order on EUR/USD via
MetaApi, on whichever MT5 account is linked to METAAPI_ACCOUNT_ID. This
does NOT check for real trading signals — it's purely to confirm the
connection and order-placement code work end to end. Run manually once,
then check your MT5 app / MetaApi dashboard for the new position.

Sends a Telegram message with the result either way.
"""

import asyncio
import os
import urllib.request
import urllib.parse

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
METAAPI_TOKEN = os.environ["METAAPI_TOKEN"]
METAAPI_ACCOUNT_ID = os.environ["METAAPI_ACCOUNT_ID"]

SYMBOL = os.environ.get("TEST_SYMBOL", "EURUSD")
LOT_SIZE = float(os.environ.get("TEST_LOT_SIZE", "0.01"))


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown",
    }).encode()
    req = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


async def run_test():
    from metaapi_cloud_sdk import MetaApi

    api = MetaApi(METAAPI_TOKEN)
    account = await api.metatrader_account_api.get_account(METAAPI_ACCOUNT_ID)

    print("Waiting for account to connect...")
    await account.wait_connected()

    connection = account.get_rpc_connection()
    await connection.connect()
    await connection.wait_synchronized()

    print(f"Placing test order: BUY {LOT_SIZE} lots {SYMBOL}...")
    result = await connection.create_market_buy_order(SYMBOL, LOT_SIZE)
    print(f"Order result: {result}")
    return result


def main():
    try:
        result = asyncio.run(run_test())
        msg = (
            f"✅ *MetaApi test order placed successfully*\n"
            f"Symbol: `{SYMBOL}`\n"
            f"Lots: `{LOT_SIZE}`\n"
            f"Result: `{result}`\n\n"
            f"Check your MT5 app for the new open position."
        )
        print(msg)
        send_telegram(msg)
    except Exception as e:
        msg = f"⚠️ *MetaApi test order FAILED*\nError: `{e}`"
        print(msg)
        try:
            send_telegram(msg)
        except Exception:
            pass
        raise


if __name__ == "__main__":
    main()
