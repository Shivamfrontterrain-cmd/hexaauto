import asyncio
import logging
import random
import re
from typing import Dict, Optional
from telethon import TelegramClient

import config
from handlers.check_handler import CheckHandler

logger = logging.getLogger("autohexa.auto_guesser")

class AutoGuesser:
    """Automated engine for the /guess 'Who's that Pokémon?' PD farming loop."""

    def __init__(self, check_handler: CheckHandler):
        self.check_handler = check_handler
        self.is_running = True
        self.tasks: Dict[str, asyncio.Task] = {}
        self.cooldown_overrides: Dict[str, float] = {}
        self.guess_events: Dict[str, asyncio.Event] = {}

    def get_guess_event(self, account_name: str) -> asyncio.Event:
        """Returns or creates the guess completion event for an account."""
        if account_name not in self.guess_events:
            event = asyncio.Event()
            event.set()
            self.guess_events[account_name] = event
        return self.guess_events[account_name]

    def mark_guess_complete(self, account_name: str):
        """Signals that the current /guess round has fully completed."""
        event = self.get_guess_event(account_name)
        if not event.is_set():
            logger.info("[%s] ✅ /guess round completed. Releasing lock for next round.", account_name)
            event.set()

    def extract_cooldown(self, text: str) -> Optional[float]:
        """Extracts cooldown time from bot messages like 'Please wait 10 seconds before guessing again'."""
        match = re.search(r"(?:wait|cooldown|again in)\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)", text, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None

    def set_cooldown(self, account_name: str, seconds: float):
        """Sets a temporary cooldown override reported by the game bot."""
        self.cooldown_overrides[account_name] = seconds
        self.mark_guess_complete(account_name)

    async def _guess_loop(self, account_name: str, client: TelegramClient, initial_delay: float):
        """Continuous /guess loop for an account to farm PD."""
        logger.info("[%s] Auto-guess worker starting in %.1f seconds...", account_name, initial_delay)
        await asyncio.sleep(initial_delay)

        while self.is_running:
            # 1. Check if account is paused due to an anti-bot check
            if account_name in self.check_handler.paused_accounts:
                logger.warning("[%s] Guesser waiting: anti-bot check resolution required...", account_name)
                await asyncio.sleep(4.0)
                continue

            # 2. Acquire lock: Clear guess event so we don't send another /guess until this round finishes!
            event = self.get_guess_event(account_name)
            event.clear()

            # 3. Send /guess command
            try:
                target_chat = config.HUNT_CHAT
                logger.info("[%s] ❓ Sending '%s' to %s...", account_name, config.GUESS_COMMAND, target_chat)
                await client.send_message(target_chat, config.GUESS_COMMAND)
            except Exception as e:
                logger.error("[%s] Failed to send /guess command: %s", account_name, e)
                event.set()
                await asyncio.sleep(5.0)
                continue

            # 4. Wait for the round to complete before sending next /guess
            timeout_sec = 15.0 if getattr(config, "GUESS_INSTA_FLASH", True) else 35.0
            try:
                logger.info("[%s] Waiting for /guess round to complete...", account_name)
                await asyncio.wait_for(event.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                logger.warning("[%s] /guess wait timed out after %.0fs. Unlocking for next cycle.", account_name, timeout_sec)
                event.set()

            # 5. Determine post-guess sleep duration (cooldown + jitter)
            if account_name in self.cooldown_overrides:
                cd = self.cooldown_overrides.pop(account_name)
                jitter = random.uniform(0.1, 0.4)
                sleep_time = cd + jitter
                logger.info("[%s] Respecting /guess cooldown: sleeping %.1fs (cd: %.1fs + jitter: %.1fs)",
                            account_name, sleep_time, cd, jitter)
            else:
                if getattr(config, "GUESS_INSTA_FLASH", True):
                    # Instant flash mode: send next /guess immediately!
                    sleep_time = random.uniform(getattr(config, "GUESS_MIN_DELAY", 0.05), getattr(config, "GUESS_MAX_DELAY", 0.20))
                    logger.info("[%s] ⚡ Instant-Flash: Round complete! Sending next '%s' in %.2fs!",
                                account_name, config.GUESS_COMMAND, sleep_time)
                else:
                    jitter = random.uniform(0.5, config.GUESS_JITTER)
                    sleep_time = config.GUESS_INTERVAL + jitter
                    logger.info("[%s] Next /guess scheduled in %.1fs...", account_name, sleep_time)

            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    def start_account(self, account_name: str, client: TelegramClient, index: int = 0):
        """Starts the auto-guess loop for an account."""
        if not config.AUTO_GUESS_ENABLED:
            logger.info("[%s] Auto-guess is disabled in config.", account_name)
            return

        if account_name in self.tasks and not self.tasks[account_name].done():
            logger.warning("[%s] Auto-guess worker is already running.", account_name)
            return

        initial_delay = 3.0 + (index * 4.0)
        task = asyncio.create_task(self._guess_loop(account_name, client, initial_delay))
        self.tasks[account_name] = task

    def stop_all(self):
        """Cancels all active guess worker tasks."""
        self.is_running = False
        for name, task in self.tasks.items():
            if not task.done():
                task.cancel()
                logger.info("[%s] Stopped auto-guess worker.", name)
