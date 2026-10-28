import asyncio
import logging
from typing import Callable, Dict, Optional

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError

import config
from core.database import db
from core.pokedex import PokedexService
from core.sprite_matcher import SpriteMatcher
from core.gemini_solver import GeminiSolver
from core.knowledge_base import KnowledgeBase
from core.notifier import EmergencyNotifier
from handlers.check_handler import CheckHandler
from handlers.spawn_handler import SpawnHandler
from handlers.battle_handler import BattleHandler
from handlers.guess_handler import GuessHandler
from core.auto_hunter import AutoHunter
from core.auto_guesser import AutoGuesser

logger = logging.getLogger("autohexa.multi_runner")

class UserEmergencyNotifier(EmergencyNotifier):
    """Dispatches emergency alerts to a specific Telegram user via bot or alert token."""

    def __init__(self, owner_id: int, bot_client: Optional[TelegramClient] = None):
        super().__init__(user_id=owner_id, bot_token=config.BOT_TOKEN)
        self.owner_id = owner_id
        self.bot_client = bot_client

    async def notify_unknown_check(
        self,
        account_name: str,
        question: str,
        options: Optional[list] = None,
        reason: str = "Unrecognized check / Low confidence"
    ):
        opts_str = "\n".join([f"• {opt}" for opt in options]) if options else "None (text input required)"
        alert_text = (
            "<b>BOT CHECK ALERT</b>\n\n"
            f"<b>Account:</b> <code>{account_name}</code>\n"
            f"<b>Reason:</b> {reason}\n\n"
            f"<b>Question:</b>\n<i>{question}</i>\n\n"
            f"<b>Detected Buttons / Options:</b>\n{opts_str}\n\n"
            "<b>Action:</b> Automation has been <b>PAUSED</b> on this account to prevent account restrictions. "
            "Please check the game chat manually and resolve the check."
        )

        logger.critical("[%s] Multi-user check alert for user %d", account_name, self.owner_id)

        # 1. Try sending directly through bot client if available
        if self.bot_client:
            try:
                await self.bot_client.send_message(self.owner_id, alert_text, parse_mode="html")
                return
            except Exception as e:
                logger.error("Failed to send alert via bot_client: %s", e)

        # 2. Fallback to standard HTTP notifier
        await super().notify_unknown_check(account_name, question, options, reason)


class AccountInstance:
    """Encapsulates a single user's running account and its workers."""

    def __init__(
        self,
        account_data: dict,
        pokedex: PokedexService,
        sprite_matcher: SpriteMatcher,
        gemini: GeminiSolver,
        kb: KnowledgeBase,
        bot_client: Optional[TelegramClient] = None,
        on_status_change: Optional[Callable] = None
    ):
        self.acc_id = account_data["id"]
        self.owner_id = account_data["owner_id"]
        self.account_name = account_data["account_name"]
        self.phone = account_data["phone"]
        self.session_string = account_data["session_string"]
        self.action_mode = account_data.get("action_mode", "guess")
        self.hunt_mode = account_data.get("hunt_mode", "catch")
        self.auto_switch = bool(account_data.get("auto_switch", 1))
        self.target_bot = account_data.get("target_bot", "HeXamonbot")
        self.hunt_chat = account_data.get("hunt_chat", "HeXamonbot")
        self.active_group_chats: set = set()
        if self.hunt_chat and (str(self.hunt_chat).startswith("-") or str(self.hunt_chat).isdigit()):
            try:
                self.active_group_chats.add(int(self.hunt_chat))
            except ValueError:
                pass

        self.on_status_change = on_status_change
        self.bot_client = bot_client
        self.is_running = False

        # Build account-specific notifier
        self.notifier = UserEmergencyNotifier(self.owner_id, bot_client=self.bot_client)
        self.check_handler = CheckHandler(knowledge_base=kb, gemini_solver=gemini, notifier=self.notifier)

        # Workers
        self.auto_hunter = AutoHunter(check_handler=self.check_handler)
        self.auto_guesser = AutoGuesser(check_handler=self.check_handler)

        # Game handlers with stats hooks
        self.battle_handler = BattleHandler(
            pokedex=pokedex,
            on_battle_end=self._on_battle_end,
            hunt_mode=self.hunt_mode,
            auto_switch=self.auto_switch
        )
        self.spawn_handler = SpawnHandler(
            on_catch_end=self._on_catch_end,
            battle_handler=self.battle_handler,
            hunt_mode=self.hunt_mode
        )
        self.battle_handler.set_hunt_mode(self.hunt_mode, self.account_name)
        self.battle_handler.set_auto_switch(self.auto_switch, self.account_name)
        self.spawn_handler.set_hunt_mode(self.hunt_mode, self.account_name)
        self.auto_hunter.set_handlers(battle_handler=self.battle_handler, spawn_handler=self.spawn_handler)

        self.guess_handler = GuessHandler(
            gemini_solver=gemini,
            sprite_matcher=sprite_matcher,
            on_guess_end=self._on_guess_end
        )

        # Telethon Client
        self.client = TelegramClient(
            StringSession(self.session_string),
            config.API_ID,
            config.API_HASH
        )

        self._register_events()

    def set_auto_switch(self, enabled: bool):
        """Updates auto-switch setting on this live instance and its battle handler."""
        self.auto_switch = enabled
        if self.battle_handler:
            self.battle_handler.set_auto_switch(enabled, self.account_name)

    def set_hunt_mode(self, mode: str):
        """Updates the hunt strategy on this live instance and its handlers."""
        self.hunt_mode = mode
        if self.battle_handler:
            self.battle_handler.set_hunt_mode(mode, self.account_name)
        if self.spawn_handler:
            self.spawn_handler.set_hunt_mode(mode, self.account_name)

    def set_target_bot(self, target_bot: str, hunt_chat: Optional[str] = None):
        """Updates target bot and hunt chat for this live account instance."""
        self.target_bot = target_bot.lstrip("@")
        self.hunt_chat = (hunt_chat or target_bot).lstrip("@")
        if hasattr(self.auto_hunter, "target_chats"):
            self.auto_hunter.target_chats[self.account_name] = self.hunt_chat
        if hasattr(self.auto_guesser, "target_chats"):
            self.auto_guesser.target_chats[self.account_name] = self.hunt_chat

    def _on_battle_end(self, account_name: str, is_catch: bool = False):
        self.auto_hunter.mark_encounter_complete(account_name)
        if is_catch:
            asyncio.create_task(db.increment_stat(self.acc_id, "catches"))
        else:
            asyncio.create_task(db.increment_stat(self.acc_id, "kills"))

    def _on_catch_end(self, account_name: str, is_catch: bool = True):
        self.auto_hunter.mark_encounter_complete(account_name)
        if is_catch:
            asyncio.create_task(db.increment_stat(self.acc_id, "catches"))

    def _on_guess_end(self, account_name: str):
        self.auto_guesser.mark_guess_complete(account_name)
        asyncio.create_task(db.increment_stat(self.acc_id, "guesses"))

    def _register_events(self):
        """Registers message listeners for the target chat and group commands."""

        @self.client.on(events.NewMessage(pattern=r"^(?:/|\.)(?:go|stop)(?:@\w+)?(?:\s+.*)?$"))
        async def on_group_command(event):
            """Handles /go and /stop commands in groups or private chats to control auto-guessing."""
            try:
                me = await self.client.get_me()
                my_id = me.id if me else None
                sender_id = event.sender_id

                is_self = event.out or (my_id and sender_id == my_id)
                is_owner = (sender_id == self.owner_id)
                is_admin = bool(
                    (config.ADMIN_USER_IDS and sender_id in config.ADMIN_USER_IDS) or
                    (config.ALERT_USER_ID and sender_id == config.ALERT_USER_ID)
                )
                has_no_admin = not config.ADMIN_USER_IDS and not config.ALERT_USER_ID

                if not (is_self or is_owner or is_admin or has_no_admin):
                    return

                text = (event.raw_text or "").strip().lower()
                chat_id = event.chat_id

                if text.startswith(("/go", ".go")):
                    logger.info("[%s] 🚀 /go command received in chat %s! Activating auto-guess...", self.account_name, chat_id)
                    self.active_group_chats.add(chat_id)
                    self.hunt_chat = str(chat_id)
                    await db.update_account_settings(self.acc_id, hunt_chat=str(chat_id))

                    if self.auto_guesser:
                        self.auto_guesser.switch_target_chat(self.account_name, self.client, chat_id)

                    try:
                        await event.reply("⚡ **[AutoHexa]** Auto-guess activated for this group! Starting /guess loop...")
                    except Exception as e:
                        logger.debug("[%s] Failed to send /go confirmation: %s", self.account_name, e)

                elif text.startswith(("/stop", ".stop")):
                    logger.info("[%s] ⏹ /stop command received in chat %s! Stopping auto-guess...", self.account_name, chat_id)
                    self.active_group_chats.discard(chat_id)
                    if self.auto_guesser:
                        self.auto_guesser.stop_account(self.account_name)

                    try:
                        await event.reply("⏹ **[AutoHexa]** Auto-guess stopped for this group.")
                    except Exception as e:
                        logger.debug("[%s] Failed to send /stop confirmation: %s", self.account_name, e)
            except Exception as e:
                logger.error("[%s] Error handling group command: %s", self.account_name, e)

        @self.client.on(events.NewMessage(incoming=True))
        @self.client.on(events.MessageEdited(incoming=True))
        async def on_bot_message(event):
            if event.message.out:
                return

            chat_id = event.chat_id

            # Determine whether this message is from an active target chat:
            is_active_chat = False
            if chat_id in self.active_group_chats:
                is_active_chat = True
            elif self.hunt_chat and (str(chat_id) == str(self.hunt_chat)):
                is_active_chat = True
            elif event.is_private:
                target_bot_lower = self.target_bot.lower()
                sender = await event.get_sender()
                sender_username = (getattr(sender, "username", None) or "").lower()
                if sender_username == target_bot_lower or str(chat_id) == str(self.target_bot):
                    is_active_chat = True

            if not is_active_chat:
                return

            # In group chats, ignore normal user messages (only listen to bots)
            if not event.is_private:
                sender = await event.get_sender()
                if not getattr(sender, "bot", False):
                    return

            if self.account_name in self.check_handler.paused_accounts:
                logger.warning("[%s] Paused due to check. Ignoring message.", self.account_name)
                return

            message = event.message
            raw_text = message.raw_text or ""

            # 1. Anti-Bot Check
            if self.check_handler.is_check_message(message):
                success = await self.check_handler.handle_check(self.client, self.account_name, message)
                if success:
                    await db.increment_stat(self.acc_id, "checks_solved")
                return

            # 2. 'Who's that Pokémon?' Silhouette Challenge
            if self.guess_handler and self.guess_handler.is_guess_challenge(message):
                await self.guess_handler.handle_challenge(self.client, self.account_name, message)
                return

            # 3. Guess Result
            if self.guess_handler and self.guess_handler.is_guess_result(message):
                self.guess_handler.handle_result(self.account_name, message)
                return

            # 4. Guess Cooldown
            if self.auto_guesser:
                gcd = self.auto_guesser.extract_cooldown(raw_text)
                if gcd and ("guess" in raw_text.lower() or "again" in raw_text.lower()):
                    self.auto_guesser.set_cooldown(self.account_name, gcd)
                    return

            # 5a. Switch prompt (Pokémon fainted or switch requested in ongoing battle)
            # Evaluated before is_battle_end so player fainting isn't mistaken for battle conclusion!
            if self.battle_handler and self.battle_handler.is_switch_prompt(message):
                await self.battle_handler.handle_battle_turn(self.client, self.account_name, message)
                return

            # 5b. Battle result / conclusion (rewards, PD earned, enemy fainted, caught, fled)
            if self.battle_handler and self.battle_handler.is_battle_end(message):
                self.battle_handler.handle_battle_result(self.account_name, message, client=self.client)
                if self.spawn_handler and hasattr(self.spawn_handler, "active_encounters"):
                    self.spawn_handler.active_encounters.pop(self.account_name, None)
                return

            # 6. Active Battle turn (moves, balls, choices in ongoing battle)
            # MUST BE CHECKED BEFORE CATCH RESULT so battle turns with 'broke free' aren't hijacked!
            if self.battle_handler and self.battle_handler.is_battle_message(message, self.account_name):
                await self.battle_handler.handle_battle_turn(self.client, self.account_name, message)
                return

            # 7. Check for catch outcome results (spawn catches or fled)
            if self.spawn_handler and self.spawn_handler.is_catch_result(message):
                self.spawn_handler.handle_catch_result(self.account_name, message, client=self.client)
                return

            # 8. Wild Pokémon encounter (outside battle)
            if self.spawn_handler and self.spawn_handler.is_spawn_message(message):
                await self.spawn_handler.handle_spawn(self.client, self.account_name, message)
                return

            # 9. Cannot hunt while in battle (Bot reported active battle)
            if any(p in raw_text.lower() for p in (
                "cannot hunt while in battle", "finish your current battle", "already in a battle"
            )):
                logger.warning("[%s] ⏳ Bot reported battle in progress. Holding hunt lock and resolving...", self.account_name)
                if self.auto_hunter:
                    event = self.auto_hunter.get_encounter_event(self.account_name)
                    event.clear()
                asyncio.create_task(self.resolve_stuck_battle(message.chat_id))
                return

            # 10. Empty hunt result
            if self.auto_hunter and any(p in raw_text.lower() for p in (
                "found nothing", "nothing appeared", "no pokemon appeared",
                "didn't find", "nothing was found", "searched the grass but",
                "not a single pokemon", "no wild pokemon"
            )):
                self.auto_hunter.mark_encounter_complete(self.account_name)
                return

            # 11. Hunt cooldown
            if self.auto_hunter:
                cd = self.auto_hunter.extract_cooldown(raw_text)
                if cd and ("hunt" in raw_text.lower() or "cooldown" in raw_text.lower() or "again" in raw_text.lower()):
                    self.auto_hunter.set_cooldown(self.account_name, cd)
                    return

    async def resolve_stuck_battle(self, chat_id: any):
        """Finds any active battle message in chat to finish it, or waits for bot state to settle."""
        try:
            recent_msgs = await self.client.get_messages(chat_id, limit=6)
            battle_msg = None
            for msg in recent_msgs:
                if self.battle_handler and self.battle_handler.is_battle_message(msg, self.account_name):
                    battle_msg = msg
                    break

            if battle_msg:
                logger.info("[%s] ⚔️ Found active battle message (id: %d)! Resuming battle turn...", self.account_name, battle_msg.id)
                self.battle_handler.active_battles[self.account_name] = {
                    "msg_id": battle_msg.id,
                    "chat_id": chat_id,
                    "last_click": asyncio.get_event_loop().time()
                }
                await self.battle_handler.handle_battle_turn(self.client, self.account_name, battle_msg)
            else:
                logger.info("[%s] ⏳ No active battle buttons found in recent messages. Waiting 2.5s for bot server to settle...", self.account_name)
                await asyncio.sleep(2.5)
                if self.auto_hunter:
                    self.auto_hunter.mark_encounter_complete(self.account_name)
        except Exception as e:
            logger.error("[%s] Error resolving stuck battle: %s", self.account_name, e)
            await asyncio.sleep(3.0)
            if self.auto_hunter:
                self.auto_hunter.mark_encounter_complete(self.account_name)

    async def start(self) -> bool:
        """Starts the Telethon client and launches automated workers."""
        if self.is_running:
            return True

        logger.info("[+] Starting account instance: %s (ID: %d)", self.account_name, self.acc_id)
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                logger.error("[!] Session for %s is no longer authorized/valid!", self.account_name)
                await self.client.disconnect()
                return False

            me = await self.client.get_me()
            logger.info("[✓] Account logged in: @%s (%d)", me.username or me.first_name, me.id)

            self.is_running = True

            # Launch Workers based on account action_mode and configured target chat
            chat_target = self.hunt_chat or self.target_bot or config.HUNT_CHAT
            if self.action_mode in ("hunt", "both"):
                self.auto_hunter.start_account(self.account_name, self.client, index=0, target_chat=chat_target)
            if self.action_mode in ("guess", "both"):
                self.auto_guesser.start_account(self.account_name, self.client, index=0, target_chat=chat_target)

            await db.set_account_active(self.acc_id, True)
            if self.on_status_change:
                self.on_status_change(self.acc_id, True)
            return True

        except FloodWaitError as e:
            logger.error("[!] FloodWait on %s: %d seconds", self.account_name, e.seconds)
            return False
        except Exception as e:
            logger.error("[!] Failed to start %s: %s", self.account_name, e)
            return False

    async def stop(self):
        """Stops workers and disconnects client."""
        if not self.is_running:
            return
        logger.info("[-] Stopping account instance: %s (ID: %d)", self.account_name, self.acc_id)
        self.is_running = False
        try:
            self.auto_hunter.stop_all()
            self.auto_guesser.stop_all()
            await self.client.disconnect()
        except Exception as e:
            logger.error("Error stopping %s: %s", self.account_name, e)
        finally:
            await db.set_account_active(self.acc_id, False)
            if self.on_status_change:
                self.on_status_change(self.acc_id, False)


class MultiRunner:
    """Coordinates and manages multiple concurrent user accounts."""

    def __init__(self, bot_client: Optional[TelegramClient] = None):
        self.bot_client = bot_client
        self.instances: Dict[int, AccountInstance] = {}

        # Shared singleton assets to conserve server RAM & CPU
        self.pokedex = PokedexService(config.POKEDEX_FILE)
        self.sprite_matcher = SpriteMatcher(config.SPRITE_CACHE_FILE)
        self.gemini = GeminiSolver(api_key=config.GEMINI_API_KEY, model=config.GEMINI_MODEL)
        self.kb = KnowledgeBase(config.KNOWN_CHECKS_FILE)

    def set_bot_client(self, bot_client: TelegramClient):
        self.bot_client = bot_client
        for inst in self.instances.values():
            inst.bot_client = bot_client
            inst.notifier.bot_client = bot_client

    def is_account_running(self, account_id: int) -> bool:
        inst = self.instances.get(account_id)
        return inst.is_running if inst else False

    async def start_account(self, account_id: int) -> bool:
        """Starts an individual account by its database ID."""
        acc_data = await db.get_account(account_id)
        if not acc_data:
            logger.error("Account ID %d not found in database.", account_id)
            return False

        if account_id in self.instances and self.instances[account_id].is_running:
            return True

        # Create or update instance
        inst = AccountInstance(
            account_data=acc_data,
            pokedex=self.pokedex,
            sprite_matcher=self.sprite_matcher,
            gemini=self.gemini,
            kb=self.kb,
            bot_client=self.bot_client,
            on_status_change=self._on_status_change
        )
        self.instances[account_id] = inst
        success = await inst.start()
        if not success:
            self.instances.pop(account_id, None)
        return success

    async def stop_account(self, account_id: int) -> bool:
        """Stops an individual account by its database ID."""
        inst = self.instances.get(account_id)
        if inst:
            await inst.stop()
            self.instances.pop(account_id, None)
            return True
        else:
            await db.set_account_active(account_id, False)
            return True

    def _on_status_change(self, account_id: int, is_running: bool):
        logger.info("Account %d status changed: %s", account_id, "RUNNING" if is_running else "STOPPED")

    async def start_all_for_user(self, owner_id: int) -> int:
        """Starts all accounts belonging to a user."""
        accounts = await db.get_user_accounts(owner_id)
        started = 0
        for acc in accounts:
            ok = await self.start_account(acc["id"])
            if ok:
                started += 1
        return started

    async def stop_all_for_user(self, owner_id: int) -> int:
        """Stops all accounts belonging to a user."""
        accounts = await db.get_user_accounts(owner_id)
        stopped = 0
        for acc in accounts:
            await self.stop_account(acc["id"])
            stopped += 1
        return stopped

    async def resume_all_active(self):
        """Called on server boot: auto-resumes any account that was active."""
        active_accounts = await db.get_all_running_accounts()
        if not active_accounts:
            logger.info("No previously active accounts to resume.")
            return

        logger.info("Auto-resuming %d previously active accounts...", len(active_accounts))
        for acc in active_accounts:
            asyncio.create_task(self.start_account(acc["id"]))

    async def stop_all(self):
        """Stops all running accounts gracefully."""
        logger.info("Stopping all multi-runner account instances...")
        for acc_id in list(self.instances.keys()):
            await self.stop_account(acc_id)

# Global singleton multi-runner
multi_runner = MultiRunner()
