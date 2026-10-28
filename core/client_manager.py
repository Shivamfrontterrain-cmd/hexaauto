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
        if self.battle_handler and not getattr(self.battle_handler, "spawn_handler", None):
            self.battle_handler.spawn_handler = self.spawn_handler
        self.guess_handler = guess_handler
        self.auto_hunter = auto_hunter
        if self.auto_hunter:
            self.auto_hunter.set_handlers(battle_handler=self.battle_handler, spawn_handler=self.spawn_handler)
        self.auto_guesser = auto_guesser
        self.clients: Dict[str, TelegramClient] = {}
        self.sessions_dir = config.SESSIONS_DIR
        self.active_group_chats: set = set()
        if config.HUNT_CHAT and (str(config.HUNT_CHAT).startswith("-") or str(config.HUNT_CHAT).isdigit()):
            try:
                self.active_group_chats.add(int(config.HUNT_CHAT))
            except ValueError:
                pass

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
        """Registers event listeners on the client for target bot interactions and group commands."""

        @client.on(events.NewMessage(pattern=r"^(?:/|\.)(?:go|stop)(?:@\w+)?(?:\s+.*)?$"))
        async def on_group_command(event):
            """Handles /go and /stop commands in groups or private chats to control auto-guessing."""
            try:
                me = await client.get_me()
                my_id = me.id if me else None
                sender_id = event.sender_id

                is_self = event.out or (my_id and sender_id == my_id)
                is_admin = bool(
                    (config.ADMIN_USER_IDS and sender_id in config.ADMIN_USER_IDS) or
                    (config.ALERT_USER_ID and sender_id == config.ALERT_USER_ID)
                )
                has_no_admin = not config.ADMIN_USER_IDS and not config.ALERT_USER_ID

                if not (is_self or is_admin or has_no_admin):
                    return

                text = (event.raw_text or "").strip().lower()
                chat_id = event.chat_id

                if text.startswith(("/go", ".go")):
                    logger.info("[%s] 🚀 /go command received in chat %s! Activating auto-guess in group...", account_name, chat_id)
                    self.active_group_chats.add(chat_id)
                    if self.auto_guesser:
                        self.auto_guesser.switch_target_chat(account_name, client, chat_id)
                    try:
                        await event.reply("⚡ **[AutoHexa]** Auto-guess activated for this group! Starting /guess loop...")
                    except Exception as e:
                        logger.debug("[%s] Failed to send /go confirmation: %s", account_name, e)

                elif text.startswith(("/stop", ".stop")):
                    logger.info("[%s] ⏹ /stop command received in chat %s! Stopping auto-guess...", account_name, chat_id)
                    self.active_group_chats.discard(chat_id)
                    if self.auto_guesser:
                        self.auto_guesser.stop_account(account_name)
                    try:
                        await event.reply("⏹ **[AutoHexa]** Auto-guess stopped for this group.")
                    except Exception as e:
                        logger.debug("[%s] Failed to send /stop confirmation: %s", account_name, e)
            except Exception as e:
                logger.error("[%s] Error handling group command: %s", account_name, e)

        @client.on(events.NewMessage(incoming=True))
        @client.on(events.MessageEdited(incoming=True))
        async def on_bot_message(event):
            # Never process outgoing messages sent by the userbot
            if event.message.out:
                return

            chat_id = event.chat_id

            # Determine whether this message is from an active target chat:
            is_active_chat = False
            if chat_id in self.active_group_chats:
                is_active_chat = True
            elif config.HUNT_CHAT and (str(chat_id) == str(config.HUNT_CHAT)):
                is_active_chat = True
            elif event.is_private:
                target_bot_lower = config.TARGET_BOT.lower()
                sender = await event.get_sender()
                sender_username = (getattr(sender, "username", None) or "").lower()
                if sender_username == target_bot_lower or str(chat_id) == str(config.TARGET_BOT):
                    is_active_chat = True

            if not is_active_chat:
                return

            # In group chats, ignore normal user messages (only listen to bots)
            if not event.is_private:
                sender = await event.get_sender()
                if not getattr(sender, "bot", False):
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

            # 5a. Switch prompt (Pokémon fainted or switch requested in ongoing battle)
            # Evaluated before is_battle_end so player fainting isn't mistaken for battle conclusion!
            if self.battle_handler and self.battle_handler.is_switch_prompt(message):
                await self.battle_handler.handle_battle_turn(client, account_name, message)
                return

            # 5b. Battle result / conclusion (rewards, PD earned, enemy fainted, caught, fled)
            if self.battle_handler and self.battle_handler.is_battle_end(message):
                self.battle_handler.handle_battle_result(account_name, message, client=client)
                if self.spawn_handler and hasattr(self.spawn_handler, "active_encounters"):
                    self.spawn_handler.active_encounters.pop(account_name, None)
                return

            # 6. Active Battle Turn (moves, balls, choices in ongoing battle)
            # MUST BE CHECKED BEFORE CATCH RESULT so battle turns with 'broke free' aren't hijacked!
            if self.battle_handler and self.battle_handler.is_battle_message(message, account_name):
                await self.battle_handler.handle_battle_turn(client, account_name, message)
                return

            # 7. Check for catch outcome results (spawn catches or fled)
            if self.spawn_handler and self.spawn_handler.is_catch_result(message):
                self.spawn_handler.handle_catch_result(account_name, message, client=client)
                return

            # 8. Check for wild Pokémon encounter / spawn (outside battle)
            if self.spawn_handler and self.spawn_handler.is_spawn_message(message):
                await self.spawn_handler.handle_spawn(client, account_name, message)
                return

            # 9. Cannot hunt while in battle (Bot reported active battle)
            if any(p in raw_text.lower() for p in (
                "cannot hunt while in battle", "finish your current battle", "already in a battle"
            )):
                logger.warning("[%s] ⏳ Bot reported battle in progress. Holding hunt lock and resolving...", account_name)
                if self.auto_hunter:
                    event = self.auto_hunter.get_encounter_event(account_name)
                    event.clear()
                asyncio.create_task(self.resolve_stuck_battle(client, account_name, message.chat_id))
                return

            # 10. Check for empty hunt result (e.g. "Nothing appeared in the grass")
            if self.auto_hunter and any(p in raw_text.lower() for p in (
                "found nothing", "nothing appeared", "no pokemon appeared",
                "didn't find", "nothing was found", "searched the grass but",
                "not a single pokemon", "no wild pokemon"
            )):
                logger.info("[%s] 🌾 No Pokémon spawned from hunt. Releasing hunt lock.", account_name)
                self.auto_hunter.mark_encounter_complete(account_name)
                return

            # 11. Check for hunt cooldown report (e.g. "wait 7 seconds")
            if self.auto_hunter:
                cd = self.auto_hunter.extract_cooldown(raw_text)
                if cd and ("hunt" in raw_text.lower() or "cooldown" in raw_text.lower() or "again" in raw_text.lower()):
                    self.auto_hunter.set_cooldown(account_name, cd)
                    return

    async def resolve_stuck_battle(self, client: TelegramClient, account_name: str, chat_id: any):
        """Finds any active battle message in chat to finish it, or waits for bot state to settle."""
        try:
            recent_msgs = await client.get_messages(chat_id, limit=6)
            battle_msg = None
            for msg in recent_msgs:
                if self.battle_handler and self.battle_handler.is_battle_message(msg, account_name):
                    battle_msg = msg
                    break

            if battle_msg:
                logger.info("[%s] ⚔️ Found active battle message (id: %d)! Resuming battle turn...", account_name, battle_msg.id)
                self.battle_handler.active_battles[account_name] = {
                    "msg_id": battle_msg.id,
                    "chat_id": chat_id,
                    "last_click": asyncio.get_event_loop().time()
                }
                await self.battle_handler.handle_battle_turn(client, account_name, battle_msg)
            else:
                logger.info("[%s] ⏳ No active battle buttons found in recent messages. Waiting 2.5s for bot server to settle...", account_name)
                await asyncio.sleep(2.5)
                if self.auto_hunter:
                    self.auto_hunter.mark_encounter_complete(account_name)
        except Exception as e:
            logger.error("[%s] Error resolving stuck battle: %s", account_name, e)
            await asyncio.sleep(3.0)
            if self.auto_hunter:
                self.auto_hunter.mark_encounter_complete(account_name)

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

