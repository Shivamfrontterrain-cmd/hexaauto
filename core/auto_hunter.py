import asyncio
import logging
import random
import re
from typing import Dict, Optional
from telethon import TelegramClient

import config
from handlers.check_handler import CheckHandler

logger = logging.getLogger("autohexa.auto_hunter")

class AutoHunter:
    """Automated hunting engine that executes loops across all accounts."""

    def __init__(self, check_handler: CheckHandler, battle_handler=None, spawn_handler=None):
        self.check_handler = check_handler
        self.battle_handler = battle_handler
        self.spawn_handler = spawn_handler
        self.is_running = True
        self.tasks: Dict[str, asyncio.Task] = {}
        self.cooldown_overrides: Dict[str, float] = {}
        self.encounter_events: Dict[str, asyncio.Event] = {}

    def set_handlers(self, battle_handler=None, spawn_handler=None):
        """Allows setting or updating handler references after initialization."""
        if battle_handler is not None:
            self.battle_handler = battle_handler
        if spawn_handler is not None:
            self.spawn_handler = spawn_handler

    def is_encounter_active(self, account_name: str) -> bool:
        """Returns True if the account is currently engaged in a wild spawn, battle, or anti-bot check."""
        if account_name in self.check_handler.paused_accounts:
            return True
        if self.battle_handler and account_name in getattr(self.battle_handler, "active_battles", {}):
            return True
        if self.spawn_handler and account_name in getattr(self.spawn_handler, "active_encounters", {}):
            return True
        return False

    def get_encounter_event(self, account_name: str) -> asyncio.Event:
        """Returns or creates the encounter completion event for an account."""
        if account_name not in self.encounter_events:
            event = asyncio.Event()
            event.set()  # Initial state is unlocked
            self.encounter_events[account_name] = event
        return self.encounter_events[account_name]

    def mark_encounter_complete(self, account_name: str):
        """Signals that the current hunt/battle has fully completed."""
        if self.battle_handler and hasattr(self.battle_handler, "active_battles"):
            self.battle_handler.active_battles.pop(account_name, None)
        if self.spawn_handler and hasattr(self.spawn_handler, "active_encounters"):
            self.spawn_handler.active_encounters.pop(account_name, None)

        event = self.get_encounter_event(account_name)
        if not event.is_set():
            logger.info("[%s] ✅ Encounter finished. Releasing hunt lock for next cycle.", account_name)
            event.set()

    def extract_cooldown(self, text: str) -> Optional[float]:
        """Extracts cooldown time from bot messages like 'Please wait 8 seconds'."""
        match = re.search(r"(?:wait|cooldown|again in)\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)", text, re.IGNORECASE)
        if match:
            return float(match.group(1))
        return None

    def set_cooldown(self, account_name: str, seconds: float):
        """Sets a temporary cooldown override reported by the game bot."""
        self.cooldown_overrides[account_name] = seconds
        # If bot reported cooldown, the hunt request was rejected, so release lock immediately
        event = self.get_encounter_event(account_name)
        if not event.is_set():
            event.set()

    async def _hunt_loop(self, account_name: str, client: TelegramClient, initial_delay: float):
        """Continuous hunt-and-catch loop for a single account."""
        logger.info("[%s] Auto-hunt worker starting in %.1f seconds...", account_name, initial_delay)
        await asyncio.sleep(initial_delay)

        while self.is_running:
            # 1. CRITICAL: Never send /hunt if an encounter or battle is already in progress!
            while self.is_running and self.is_encounter_active(account_name):
                logger.info("[%s] ⏳ Active encounter/battle in progress. Holding hunt command...", account_name)
                await asyncio.sleep(1.0)

            if not self.is_running:
                break

            # 2. Acquire lock: Clear encounter event so we don't hunt again until this one completes!
            event = self.get_encounter_event(account_name)
            event.clear()

            # 3. Send the hunt command
            try:
                target_chat = config.HUNT_CHAT
                logger.info("[%s] 🏹 Sending hunt command '%s' to %s...", account_name, config.HUNT_COMMAND, target_chat)
                await client.send_message(target_chat, config.HUNT_COMMAND)
            except Exception as e:
                logger.error("[%s] Failed to send hunt command: %s", account_name, e)
                event.set()
                await asyncio.sleep(5.0)
                continue

            # 4. CRITICAL: Wait for current hunt encounter / battle to COMPLETELY finish before proceeding!
            while self.is_running:
                try:
                    logger.info("[%s] Waiting for current encounter/battle to finish before sending next /hunt...", account_name)
                    await asyncio.wait_for(event.wait(), timeout=60.0)
                except asyncio.TimeoutError:
                    if self.is_encounter_active(account_name):
                        logger.warning("[%s] ⏳ Encounter/battle still actively in progress after 60s. Continuing to wait...", account_name)
                        continue
                    else:
                        logger.warning("[%s] Encounter wait timed out after 60s with no active encounter. Unlocking for next cycle.", account_name)
                        event.set()
                        break

                # Confirm that all battle / spawn handlers have truly finished
                if self.is_encounter_active(account_name):
                    logger.info("[%s] ⏳ Hunt lock notified but encounter/battle is still active. Waiting...", account_name)
                    event.clear()
                    await asyncio.sleep(0.5)
                    continue
                else:
                    break

            # 5. Determine post-encounter sleep duration (cooldown + human jitter)
            if account_name in self.cooldown_overrides:
                cd = self.cooldown_overrides.pop(account_name)
                jitter = random.uniform(0.1, 0.4)
                sleep_time = cd + jitter
                logger.info("[%s] Respecting bot cooldown: sleeping %.1fs (cd: %.1fs + jitter: %.1fs)",
                            account_name, sleep_time, cd, jitter)
            else:
                if getattr(config, "HUNT_INSTA_FLASH", True):
                    # Instant flash mode: send next /hunt immediately!
                    sleep_time = random.uniform(getattr(config, "HUNT_MIN_DELAY", 0.05), getattr(config, "HUNT_MAX_DELAY", 0.20))
                    logger.info("[%s] ⚡ Instant-Flash: Encounter finished! Sending next '%s' in %.2fs!",
                                account_name, config.HUNT_COMMAND, sleep_time)
                else:
                    jitter = random.uniform(0.5, config.HUNT_JITTER)
                    sleep_time = config.HUNT_INTERVAL + jitter
                    logger.info("[%s] Next hunt scheduled in %.1fs...", account_name, sleep_time)

            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    def start_account(self, account_name: str, client: TelegramClient, index: int = 0):
        """Starts the auto-hunt loop for an account with a staggered start."""
        if account_name in self.tasks and not self.tasks[account_name].done():
            logger.warning("[%s] Auto-hunt worker is already running.", account_name)
            return

        initial_delay = 2.0 + (index * 3.5)
        task = asyncio.create_task(self._hunt_loop(account_name, client, initial_delay))
        self.tasks[account_name] = task

    def stop_all(self):
        """Cancels all active hunt worker tasks."""
        self.is_running = False
        for name, task in self.tasks.items():
            if not task.done():
                task.cancel()
                logger.info("[%s] Stopped auto-hunt worker.", name)
