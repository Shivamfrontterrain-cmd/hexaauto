import asyncio
import logging
import re
import signal
import sys
from pathlib import Path

import config
from core.knowledge_base import KnowledgeBase
from core.gemini_solver import GeminiSolver
from core.notifier import EmergencyNotifier
from core.pokedex import PokedexService
from core.sprite_matcher import SpriteMatcher
from handlers.check_handler import CheckHandler
from handlers.spawn_handler import SpawnHandler
from handlers.battle_handler import BattleHandler
from handlers.guess_handler import GuessHandler
from core.auto_hunter import AutoHunter
from core.auto_guesser import AutoGuesser
from core.client_manager import ClientManager

# Setup formatted logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s: %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("autohexa.main")

async def main():
    logger.info("Active Hunt Category: '%s' (%s)",
                config.HUNT_MODE.upper(),
                "Defeat wild Pokémon for PD & EXP" if config.HUNT_MODE == "kill" else "Catch Pokémon with Pokéballs")
    logger.info("Auto-Guess System: %s (Command: '%s', Interval: %.1fs)",
                "ENABLED" if config.AUTO_GUESS_ENABLED else "DISABLED",
                config.GUESS_COMMAND, config.GUESS_INTERVAL)

    # Validate essential credentials
    if not config.API_ID or not config.API_HASH:
        logger.error("TELEGRAM_API_ID or TELEGRAM_API_HASH not set in .env! Please configure .env.")
        sys.exit(1)

    logger.info("Operating Mode: '%s' (/guess only, /hunt only, or both)", config.ACTION_MODE.upper())

    # Initialize components
    gemini = GeminiSolver(api_key=config.GEMINI_API_KEY, model=config.GEMINI_MODEL)

    # Validate Gemini API Key
    is_valid, validation_msg = await gemini.validate_api_key()
    if not is_valid:
        print("\n" + "=" * 75)
        print(" [!] CRITICAL WARNING: YOUR GEMINI_API_KEY IS INVALID OR NOT CONFIGURED!")
        print(f" [!] Error details: {validation_msg}")
        print(" [!] Current key in .env:")
        print(f"     '{config.GEMINI_API_KEY[:15]}...' (length: {len(config.GEMINI_API_KEY)})")
        print("\n [!] HOW TO FIX:")
        print("     1. Open your browser and go to: https://aistudio.google.com/app/apikey")
        print("     2. Sign in with your Google account and click 'Create API Key' (100% Free).")
        print("     3. Copy the key (it starts with 'AIzaSy...').")
        print("     4. Paste it into .env as: GEMINI_API_KEY=AIzaSyYourActualKeyHere")
        print("=" * 75 + "\n")
    else:
        logger.info("✓ Gemini AI Studio API key verified and active (%s).", config.GEMINI_MODEL)

    pokedex = PokedexService(config.POKEDEX_FILE)
    kb = KnowledgeBase(config.KNOWN_CHECKS_FILE)
    notifier = EmergencyNotifier(user_id=config.ALERT_USER_ID, bot_token=config.ALERT_BOT_TOKEN)
    check_handler = CheckHandler(knowledge_base=kb, gemini_solver=gemini, notifier=notifier)
    
    auto_hunter = AutoHunter(check_handler=check_handler)
    auto_guesser = AutoGuesser(check_handler=check_handler)

    battle_handler = BattleHandler(
        pokedex=pokedex,
        on_battle_end=auto_hunter.mark_encounter_complete
    )
    spawn_handler = SpawnHandler(
        on_catch_end=auto_hunter.mark_encounter_complete,
        battle_handler=battle_handler
    )
    sprite_matcher = SpriteMatcher(config.SPRITE_CACHE_FILE)
    guess_handler = GuessHandler(
        gemini_solver=gemini,
        sprite_matcher=sprite_matcher,
        on_guess_end=auto_guesser.mark_guess_complete
    )

    manager = ClientManager(
        check_handler=check_handler,
        spawn_handler=spawn_handler,
        battle_handler=battle_handler,
        guess_handler=guess_handler,
        auto_hunter=auto_hunter,
        auto_guesser=auto_guesser
    )

    # Check for available accounts
    available_sessions = sorted(manager.discover_sessions(), key=lambda p: p.stem)
    if not available_sessions:
        logger.warning("No .session files found in '%s'!", config.SESSIONS_DIR)
        print("\n" + "=" * 60)
        print(" [!] No Telegram accounts configured yet.")
        print(" Run: .venv\\Scripts\\python.exe add_account.py")
        print(" to log into your account(s) and generate sessions.")
        print("=" * 60 + "\n")
        return

    # Interactive Account Selection Prompt
    selected_sessions = []
    if len(available_sessions) == 1:
        selected_sessions = available_sessions
        logger.info("Using configured account: '%s'", selected_sessions[0].stem)
    else:
        print("\n" + "=" * 60)
        print(" AutoHexa - Select Telegram Account(s) to Run")
        print("=" * 60)
        for idx, s in enumerate(available_sessions, start=1):
            print(f" [{idx}] {s.stem}")
        print(f" [A] Run ALL accounts ({len(available_sessions)} accounts)")
        print("=" * 60)

        try:
            choice = input(f"Select account number(s) (e.g. 1, or 1,2) [default: A]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not choice or choice in ("a", "all"):
            selected_sessions = available_sessions
        else:
            chosen_indices = []
            for part in re.split(r"[\s,]+", choice):
                if part.isdigit():
                    num = int(part)
                    if 1 <= num <= len(available_sessions):
                        chosen_indices.append(num - 1)
                    else:
                        print(f"[!] Warning: Account number {num} is out of range.")
                else:
                    matching = [i for i, s in enumerate(available_sessions) if s.stem.lower() == part.lower()]
                    if matching:
                        chosen_indices.extend(matching)

            seen = set()
            for idx in chosen_indices:
                if idx not in seen:
                    seen.add(idx)
                    selected_sessions.append(available_sessions[idx])

            if not selected_sessions:
                print("[!] Invalid selection. Defaulting to ALL accounts.")
                selected_sessions = available_sessions

    # Interactive Hunt Category Selection (when hunting is enabled)
    if config.ACTION_MODE in ("hunt", "both"):
        current_cat = config.HUNT_MODE
        default_num = "1" if current_cat == "kill" else "2"
        print("\n" + "=" * 60)
        print(" AutoHexa - Select Hunt Category")
        print("=" * 60)
        print(f" [1] Kill  - Defeat wild Pokémon for maximum PD & EXP {'[ACTIVE]' if current_cat == 'kill' else ''}")
        print(f" [2] Catch - Throw Pokéballs to catch Pokémon (Repeat/Ultra/Poke) {'[ACTIVE]' if current_cat == 'catch' else ''}")
        print("=" * 60)
        try:
            h_choice = input(f"Select category [1: Kill / 2: Catch] [default: {default_num}]: ").strip().lower()
            if h_choice in ("1", "kill"):
                config.HUNT_MODE = "kill"
                config.BATTLE_SYSTEM = "kill"
            elif h_choice in ("2", "catch", "hybrid"):
                config.HUNT_MODE = "catch"
                config.BATTLE_SYSTEM = "catch"
        except (EOFError, KeyboardInterrupt):
            pass
        logger.info("Active Hunt Category: '%s'", config.HUNT_MODE.upper())

    account_names = [s.stem for s in selected_sessions]
    logger.info("Starting %d account(s): %s targeting @%s...",
                len(selected_sessions), ", ".join(f"'{name}'" for name in account_names), config.TARGET_BOT)
    await manager.start_all(selected_sessions)

    # Graceful shutdown handler
    stop_event = asyncio.Event()

    def handle_exit():
        logger.info("Shutdown signal received. Exiting...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, handle_exit)
        except NotImplementedError:
            # Signal handlers not implemented on Windows event loop for some signals
            pass

    try:
        await manager.run_until_disconnected()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        await manager.stop_all()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Process terminated by user.")
