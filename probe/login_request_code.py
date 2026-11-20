"""Request a Telegram login code. Usage: python login_request_code.py [test|main]"""
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

ACCOUNTS = {
    "test": ("TELEGRAM_PHONE", "test_account"),
    "main": ("TELEGRAM_MAIN_PHONE", "main_account"),
}


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "test"
    phone_var, session_name = ACCOUNTS[name]
    phone = os.environ[phone_var]
    session = str(BASE / session_name)
    state = BASE / f"login_state_{name}.json"

    client = TelegramClient(session, API_ID, API_HASH)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"ALREADY_LOGGED_IN:{me.id}:{me.username}")
        await client.disconnect()
        return

    result = await client.send_code_request(phone)
    state.write_text(
        json.dumps({"phone": phone, "phone_code_hash": result.phone_code_hash}),
        encoding="utf-8",
    )
    print("CODE_SENT")
    print(f"type:{result.type}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
