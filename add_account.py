import asyncio
import sys
from pathlib import Path
from core.client_manager import ClientManager
from handlers.check_handler import CheckHandler
from core.knowledge_base import KnowledgeBase
from core.gemini_solver import GeminiSolver
from core.notifier import EmergencyNotifier
import config

async def main():
    print("=" * 60)
    print("AutoHexa - Telegram Multi-Account Session Setup")
    print("=" * 60)

    if not config.API_ID or not config.API_HASH:
        print("[!] ERROR: Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in your .env file first.")
        sys.exit(1)

    account_name = input("Enter a custom name for this account (e.g. account1, bot_alt): ").strip()
    if not account_name:
        account_name = "account1"

    phone = input("Enter the phone number with country code (e.g. +1234567890): ").strip()
    if not phone:
        print("[!] Phone number is required.")
        sys.exit(1)

    kb = KnowledgeBase(config.KNOWN_CHECKS_FILE)
    solver = GeminiSolver(config.GEMINI_API_KEY, config.GEMINI_MODEL)
    notifier = EmergencyNotifier(config.ALERT_USER_ID, config.ALERT_BOT_TOKEN)
    handler = CheckHandler(kb, solver, notifier)
    manager = ClientManager(handler)

    try:
        await manager.add_new_account(account_name, phone)
        print(f"\n[✓] Session successfully saved to: sessions/{account_name}.session")
        print("[+] You can run this script again to add more accounts, or start main.py!")
    except Exception as e:
        print(f"\n[!] Failed to login: {e}")

if __name__ == "__main__":
    asyncio.run(main())
