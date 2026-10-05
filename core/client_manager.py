import asyncio
import logging
from pathlib import Path
from typing import Dict, List
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, SessionPasswordNeededError

from handlers.check_handler import CheckHandler
from handlers.spawn_handler import SpawnHandler
from handlers.battle_handler import BattleHandler
from handlers.guess_handler import GuessHandler
from core.auto_hunter import AutoHunter
from core.auto_guesser import AutoGuesser
import config

logger = logging.getLogger("autohexa.client_manager")

class ClientManager:
    """Manages multi-account Telethon clients, sessions, and message routing."""

    def __init__(
        self,
        check_handler: CheckHandler,
        spawn_handler: Optional[SpawnHandler] = None,
        battle_handler: Optional[BattleHandler] = None,
        guess_handler: Optional[GuessHandler] = None,
        auto_hunter: Optional[AutoHunter] = None,
        auto_guesser: Optional[AutoGuesser] = None
    ):
        self.check_handler = check_handler
        self.spawn_handler = spawn_handler
        self.battle_handler = battle_handler
        if self.spawn_handler and not getattr(self.spawn_handler, "battle_handler", None):
            self.spawn_handler.battle_handler = self.battle_handler
        self.guess_handler = guess_handler
        self.auto_hunter = auto_hunter
        self.auto_guesser = auto_guesser
        self.clients: Dict[str, TelegramClient] = {}
        self.sessions_dir = config.SESSIONS_DIR

    def discover_sessions(self) -> List[Path]:
        """Finds all existing .session files in the sessions/ folder."""
        return list(self.sessions_dir.glob("*.session"))

    async def add_new_account(self, session_name: str, phone: str):
        """Interactively authenticates and adds a new Telegram account."""
        session_path = self.sessions_dir / session_name
        client = TelegramClient(str(session_path), config.API_ID, config.API_HASH)
        await client.connect()

        if not await client.is_user_authorized():
            print(f"\n[+] Requesting login code for {phone}...")
            sent = await client.send_code_request(phone)
            code = input(f"[?] Enter the code sent to {phone} in Telegram: ")
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                password = input("[?] Two-Step Verification enabled. Enter 2FA password: ")
                await client.sign_in(password=password)

        me = await client.get_me()
        print(f"[✓] Successfully logged in as @{me.username or me.first_name} (ID: {me.id})")
        await client.disconnect()

    def _register_events(self, client: TelegramClient, account_name: str):
        """Registers event listeners on the client for target bot interactions."""
        target_chats = [config.TARGET_BOT]
        if config.HUNT_CHAT and config.HUNT_CHAT.lower() != config.TARGET_BOT.lower():
            target_chats.append(config.HUNT_CHAT)

        @client.on(events.NewMessage(chats=target_chats, incoming=True))
        @client.on(events.MessageEdited(chats=target_chats, incoming=True))
        async def on_bot_message(event):
            # Never process outgoing messages sent by the userbot
            if event.message.out:
                return

            # Check if this account is currently paused due to an unhandled check
            if account_name in self.check_handler.paused_accounts:
                logger.warning("[%s] Account is currently paused. Ignoring message.", account_name)
                return

            message = event.message
            raw_text = message.raw_text or ""

            # 1. Anti-Bot Check has absolute highest priority
            if self.check_handler.is_check_message(message):
                success = await self.check_handler.handle_check(client, account_name, message)
                if not success:
                    logger.error("[%s] Anti-bot check could not be resolved safely.", account_name)
                return

            # 2. 'Who's that Pokémon?' Silhouette Challenge (/guess)
            if self.guess_handler and self.guess_handler.is_guess_challenge(message):
                await self.guess_handler.handle_challenge(client, account_name, message)
                return

            # 3. 'Who's that Pokémon?' Result / Reward
            if self.guess_handler and self.guess_handler.is_guess_result(message):
                self.guess_handler.handle_result(account_name, message)
                return

            # 4. Check for /guess cooldown report
            if self.auto_guesser:
                gcd = self.auto_guesser.extract_cooldown(raw_text)
                if gcd and ("guess" in raw_text.lower() or "again" in raw_text.lower()):
                    self.auto_guesser.set_cooldown(account_name, gcd)
                    return

            # 5. Battle result / conclusion (rewards, PD earned, fainted)
            if self.battle_handler and self.battle_handler.is_battle_end(message):
                self.battle_handler.handle_battle_result(account_name, message)
                return

            # 6. Check for catch outcome results
            if self.spawn_handler and self.spawn_handler.is_catch_result(message):
                self.spawn_handler.handle_catch_result(account_name, message)
                return

            # 7. Check for wild Pokémon encounter / spawn (outside battle)
            if self.spawn_handler and self.spawn_handler.is_spawn_message(message):
                await self.spawn_handler.handle_spawn(client, account_name, message)
                return

            # 8. Active Battle Turn (moves, balls, choices)
            if self.battle_handler and self.battle_handler.is_battle_message(message, account_name):
                await self.battle_handler.handle_battle_turn(client, account_name, message)
                return

            # 9. Check for hunt cooldown report (e.g. "wait 7 seconds")
            if self.auto_hunter:
                cd = self.auto_hunter.extract_cooldown(raw_text)
                if cd and ("hunt" in raw_text.lower() or "cooldown" in raw_text.lower()):
                    self.auto_hunter.set_cooldown(account_name, cd)
                    return

    async def start_all(self, session_files: Optional[List[Path]] = None):
        """Discovers and starts multi-account clients concurrently."""
        if session_files is None:
            session_files = self.discover_sessions()
        if not session_files:
            logger.warning("No session files found in '%s' directory.", self.sessions_dir)
            return

        logger.info("Found %d session(s). Initializing clients...", len(session_files))

        tasks = []
        for s_file in session_files:
            account_name = s_file.stem
            client = TelegramClient(str(s_file), config.API_ID, config.API_HASH)
            self.clients[account_name] = client
            self._register_events(client, account_name)

            async def init_client(c=client, name=account_name):
                try:
                    await c.start()
                    me = await c.get_me()
                    logger.info("[✓] Account '%s' active: @%s (ID: %d)", name, me.username or me.first_name, me.id)
                except FloodWaitError as e:
                    logger.error("[!] FloodWait on account '%s': sleep for %d seconds", name, e.seconds)
                except Exception as e:
                    logger.error("[!] Failed to start account '%s': %s", name, e)

            tasks.append(init_client())

        await asyncio.gather(*tasks)
        
        # Launch background auto-hunting only if ACTION_MODE is 'hunt' or 'both'
        if self.auto_hunter and config.ACTION_MODE in ("hunt", "both") and config.AUTO_HUNT_ENABLED:
            logger.info("🏹 Launching Auto-Hunter workers (ACTION_MODE='%s')...", config.ACTION_MODE)
            for idx, (name, client) in enumerate(self.clients.items()):
                self.auto_hunter.start_account(name, client, index=idx)
        else:
            logger.info("🏹 Auto-Hunter is DISABLED (ACTION_MODE='%s').", config.ACTION_MODE)

        # Launch background auto-guessing only if ACTION_MODE is 'guess' or 'both'
        if self.auto_guesser and config.ACTION_MODE in ("guess", "both") and config.AUTO_GUESS_ENABLED:
            logger.info("❓ Launching Auto-Guesser workers (ACTION_MODE='%s')...", config.ACTION_MODE)
            for idx, (name, client) in enumerate(self.clients.items()):
                self.auto_guesser.start_account(name, client, index=idx)
        else:
            logger.info("❓ Auto-Guesser is DISABLED (ACTION_MODE='%s').", config.ACTION_MODE)

        logger.info("Active accounts monitoring @%s. Ready!", config.TARGET_BOT)

    async def run_until_disconnected(self):
        """Runs all clients in parallel until interrupted."""
        if not self.clients:
            return
        await asyncio.gather(*(c.run_until_disconnected() for c in self.clients.values()))

    async def stop_all(self):
        """Gracefully disconnects all active clients and stops hunt and guess workers."""
        logger.info("Stopping all workers and clients...")
        if self.auto_hunter:
            self.auto_hunter.stop_all()
        if self.auto_guesser:
            self.auto_guesser.stop_all()
        for name, client in self.clients.items():
            try:
                await client.disconnect()
                logger.info("Disconnected '%s'", name)
            except Exception as e:
                logger.error("Error disconnecting '%s': %s", name, e)

