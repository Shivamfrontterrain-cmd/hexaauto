import asyncio
import logging
import random
import re
from typing import Dict, Optional
from telethon import TelegramClient
from telethon.tl.custom.message import Message

import config

logger = logging.getLogger("autohexa.spawn_handler")

SPAWN_TRIGGER_PHRASES = [
    "wild",
    "appeared",
    "a wild",
    "what will you do",
    "choose your ball",
    "throw a ball",
    "catch"
]

CATCH_RESULT_PHRASES = [
    "congratulations",
    "caught",
    "gotcha",
    "broke free",
    "fled",
    "escaped",
    "missed"
]

class SpawnHandler:
    """Detects wild Pokémon encounters and executes automated catching."""

    def __init__(self, on_catch_end=None):
        self.on_catch_end = on_catch_end
        self.stats = {
            "encountered": 0,
            "caught": 0,
            "fled": 0
        }

    def is_spawn_message(self, message: Message) -> bool:
        """Checks if the message represents a wild Pokémon encounter."""
        text = (message.raw_text or "").lower()
        if any(phrase in text for phrase in SPAWN_TRIGGER_PHRASES):
            # Also check if it has buttons or mentions a pokemon/ball
            if message.buttons or "level" in text or "appeared" in text or "wild" in text:
                return True
        return False

    def is_catch_result(self, message: Message) -> bool:
        """Checks if the message reports a catch result (success or failure)."""
        text = (message.raw_text or "").lower()
        return any(phrase in text for phrase in CATCH_RESULT_PHRASES)

    def extract_pokemon_name(self, text: str) -> str:
        """Attempts to extract the spawned Pokémon name."""
        match = re.search(r"(?:wild|a wild)\s+([A-Za-z0-9\-]+)", text, re.IGNORECASE)
        if match:
            return match.group(1).title()
        return "Unknown Pokémon"

    async def handle_spawn(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """Selects the best available Pokéball button and catches the Pokémon."""
        if not config.AUTO_CATCH_ENABLED:
            logger.info("[%s] Auto-catch is disabled in config. Skipping encounter.", account_name)
            return False

        poke_name = self.extract_pokemon_name(message.raw_text or "")
        self.stats["encountered"] += 1
        logger.info("[%s] 🌟 Wild encounter: %s! Inspecting catch options...", account_name, poke_name)

        if not message.buttons:
            logger.warning("[%s] No buttons attached to spawn message. Checking if text command is required...", account_name)
            return False

        # Map available buttons
        button_map = {}
        for row in message.buttons:
            for btn in row:
                button_map[btn.text.strip().lower()] = btn

        logger.debug("[%s] Available buttons: %s", account_name, list(button_map.keys()))

        # Prioritize balls according to PREFERRED_BALLS
        chosen_button = None
        chosen_ball_name = None

        for pref in config.PREFERRED_BALLS:
            pref_lower = pref.lower().replace(" ", "")
            for label, btn in button_map.items():
                clean_label = label.replace(" ", "")
                if pref_lower in clean_label:
                    chosen_button = btn
                    chosen_ball_name = pref
                    break
            if chosen_button:
                break

        # Fallback: if no specific ball matched, check for any button with "ball" or "catch"
        if not chosen_button:
            for label, btn in button_map.items():
                if "ball" in label or "catch" in label or "throw" in label:
                    chosen_button = btn
                    chosen_ball_name = btn.text.strip()
                    break

        # If still no ball button, click the first button if it looks like a catch action
        if not chosen_button and message.buttons:
            first_btn = message.buttons[0][0]
            logger.info("[%s] No standard ball found, defaulting to first button: '%s'", account_name, first_btn.text)
            chosen_button = first_btn
            chosen_ball_name = first_btn.text.strip()

        if chosen_button:
            # Simulated human reaction delay
            delay = random.uniform(1.2, 2.6)
            logger.info("[%s] Throwing %s in %.2fs...", account_name, chosen_ball_name, delay)
            await asyncio.sleep(delay)
            try:
                await chosen_button.click()
                logger.info("[%s] Threw %s at %s!", account_name, chosen_ball_name, poke_name)
                return True
            except Exception as e:
                logger.error("[%s] Failed to click catch button: %s", account_name, e)
                return False

        logger.warning("[%s] Could not determine a catch action for %s.", account_name, poke_name)
        return False

    def handle_catch_result(self, account_name: str, message: Message):
        """Processes the outcome of a catch attempt and updates statistics."""
        text = (message.raw_text or "").lower()
        if "caught" in text or "congratulations" in text or "gotcha" in text:
            self.stats["caught"] += 1
            logger.info("[%s] 🎉 CATCH SUCCESS! Total caught: %d/%d",
                        account_name, self.stats["caught"], self.stats["encountered"])
        elif "broke free" in text or "fled" in text or "escaped" in text:
            self.stats["fled"] += 1
            logger.info("[%s] 💨 Pokémon fled or broke free. Total fled: %d",
                        account_name, self.stats["fled"])

        # Release encounter lock so AutoHunter can proceed to the next cycle
        if self.on_catch_end:
            try:
                self.on_catch_end(account_name)
            except Exception as e:
                logger.error("[%s] Error in on_catch_end callback: %s", account_name, e)
