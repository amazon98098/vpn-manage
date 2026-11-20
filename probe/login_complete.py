"""Complete Telegram login. Usage: python login_complete.py <test|main> <CODE> [2FA_PASSWORD]"""
import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]

SESSIONS = {"test": "test_account", "main": "main_account"}


async def main() -> None:
    if len(sys.argv) < 3:
        print("USAGE: login_complete.py <test|main> <CODE> [2FA_PASSWORD]")
        return

    name = sys.argv[1]
    code = sys.argv[2].strip()
    password = sys.argv[3].strip() if len(sys.argv) > 3 else None

    state_file = BASE / f"login_state_{name}.json"
    if not state_file.exists() and name == "test":
        state_file = BASE / "login_state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))

    client = TelegramClient(str(BASE / SESSIONS[name]), API_ID, API_HASH)
    await client.connect()

    try:
        await client.sign_in(
            phone=state["phone"],
            code=code,
            phone_code_hash=state["phone_code_hash"],
        )
    except SessionPasswordNeededError:
        if not password:
            print("NEED_2FA_PASSWORD")
            await client.disconnect()
            return
        await client.sign_in(password=password)

    me = await client.get_me()
    print(f"LOGIN_OK:{me.id}:{me.username}:{me.first_name}")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
