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

    existing_sessions = sorted([p.stem for p in config.SESSIONS_DIR.glob("*.session")])
    if existing_sessions:
        print(f"[+] Existing account sessions in sessions/:")
        for s in existing_sessions:
            print(f"    • {s}")
        print()

    # Determine next available account name (account1, account2, ...)
    idx = 1
    while f"account{idx}" in existing_sessions:
        idx += 1
    default_name = f"account{idx}"

    account_name = input(f"Enter a custom name for this account [default: {default_name}]: ").strip()
    if not account_name:
        account_name = default_name

    session_file = config.SESSIONS_DIR / f"{account_name}.session"
    if session_file.exists():
        print(f"\n[!] WARNING: '{account_name}.session' already exists!")
        print("    If 'main.py' is currently running in another terminal, you must")
        print("    stop it (Ctrl+C) first, or choose a new name like 'account2'.\n")
        confirm = input(f"Do you want to overwrite '{account_name}'? (y/N): ").strip().lower()
        if confirm != "y":
            print(f"[*] Cancelled. Please run add_account.py again and use '{default_name}'.")
            return

    phone = input("\nEnter the phone number with country code (e.g. +1234567890): ").strip()
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
        print(f"[+] Total accounts configured: {len(list(config.SESSIONS_DIR.glob('*.session')))}")
        print("[+] You can run this script again to add more accounts, or start main.py!")
    except Exception as e:
        err_str = str(e).lower()
        if "database is locked" in err_str:
            print(f"\n[!] Failed to login: database is locked!")
            print(f"    • Cause: 'main.py' is currently running and has locked '{account_name}.session'.")
            print(f"    • Fix: Stop main.py (Ctrl+C) or name this account '{default_name}' instead.")
        else:
            print(f"\n[!] Failed to login: {e}")

if __name__ == "__main__":
    asyncio.run(main())
