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
    "missed",
    "ran away",
    "flew away",
    "vanished",
    "disappeared",
    "was caught"
]

class SpawnHandler:
    """Detects wild Pokémon encounters and executes automated catching or battle initiation."""

    def __init__(self, on_catch_end=None, battle_handler=None):
        self.on_catch_end = on_catch_end
        self.battle_handler = battle_handler
        self.stats = {
            "encountered": 0,
            "caught": 0,
            "fled": 0
        }

    def is_spawn_message(self, message: Message) -> bool:
        """Checks if the message represents a wild Pokémon encounter outside of battle."""
        if not message:
            return False

        text = (message.raw_text or "").lower()

        # If it's a catch result or battle message, do not treat as a fresh spawn
        if self.is_catch_result(message):
            return False
        if any(b in text for b in ("battle begins", "battle started", "current turn:", "turn:")):
            return False

        # If buttons contain battle utility or action buttons, it belongs to BattleHandler!
        buttons = message.buttons or []
        if buttons:
            button_texts = [btn.text.strip().lower() for row in buttons for btn in row]
            battle_btn_keywords = (
                "run", "pokemons", "pokemon", "switch", "bag", "items",
                "fight", "attack", "moves", "continue", "next"
            )
            if any(any(k in b for k in battle_btn_keywords) for b in button_texts):
                return False

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

    async def click_battle_button_until_started(
        self,
        client: TelegramClient,
        account_name: str,
        message: Message,
        battle_btn: any,
        max_attempts: int = 10,
        click_interval: float = 0.8
    ) -> bool:
        """
        Clicks the Battle button repeatedly until the battle actually begins.
        Fixes dropped Telegram callback queries and latency.
        """
        chat_id = message.chat_id
        msg_id = message.id
        current_btn = battle_btn

        logger.info("[%s] ⚔️ Starting Battle clicker for message %d (max %d attempts)...",
                    account_name, msg_id, max_attempts)

        for attempt in range(1, max_attempts + 1):
            # Check 1: Has BattleHandler already registered this battle as active?
            if self.battle_handler and account_name in self.battle_handler.active_battles:
                logger.info("[%s] ⚔️ Battle confirmed active in BattleHandler!", account_name)
                return True

            # Check 2: Click the Battle button
            try:
                btn_text = getattr(current_btn, "text", "Battle").strip()
                logger.info("[%s] ⚔️ Clicking Battle button '%s' (attempt %d/%d)...",
                            account_name, btn_text, attempt, max_attempts)
                await current_btn.click()
            except Exception as e:
                logger.warning("[%s] Error clicking Battle button on attempt %d: %s",
                               account_name, attempt, e)

            # Check 3: Wait for Telegram callback & message update
            await asyncio.sleep(click_interval)

            # Check 4: Check if BattleHandler now has this account in active_battles
            if self.battle_handler and account_name in self.battle_handler.active_battles:
                logger.info("[%s] ⚔️ Battle successfully started and registered in active_battles! (attempt %d)",
                            account_name, attempt)
                return True

            # Check 5: Fetch latest message from Telegram to check if message updated
            try:
                latest_msg = await client.get_messages(chat_id, ids=msg_id)
            except Exception as e:
                logger.debug("[%s] Could not re-fetch message %d: %s", account_name, msg_id, e)
                latest_msg = None

            if latest_msg:
                latest_text = (latest_msg.raw_text or "").lower()

                battle_started_phrases = (
                    "battle begins", "battle started", "current turn:", "turn:", "your turn"
                )
                has_hp_indicator = bool(re.search(r"hp[:\s]*\d+\s*/\s*\d+", latest_text, re.IGNORECASE))
                has_battle_phrase = any(phrase in latest_text for phrase in battle_started_phrases)

                latest_buttons = latest_msg.buttons or []
                flattened_buttons = [b for row in latest_buttons for b in row]
                button_texts = [b.text.strip().lower() for b in flattened_buttons]

                has_battle_controls = any(any(k in b for k in (
                    "run", "pokemons", "pokemon", "switch", "bag", "moves", "scratch", "tackle", "ember"
                )) for b in button_texts)
                has_battle_btn = any(any(k in b for k in ("battle", "battles", "fight")) for b in button_texts)

                # Has battle concluded or started?
                if self.battle_handler:
                    if self.battle_handler.is_battle_end(latest_msg):
                        logger.info("[%s] ⚔️ Battle ended immediately on msg_id %d!", account_name, msg_id)
                        self.battle_handler.handle_battle_result(account_name, latest_msg)
                        return True
                    if self.battle_handler.is_battle_message(latest_msg, account_name) or has_battle_phrase or has_hp_indicator or has_battle_controls:
                        logger.info("[%s] ⚔️ Battle confirmed started on msg_id %d (attempt %d)!", account_name, msg_id, attempt)
                        await self.battle_handler.handle_battle_turn(client, account_name, latest_msg)
                        return True

                # If battle button is no longer present on the message, it transitioned!
                if not has_battle_btn and (latest_buttons or latest_text != (message.raw_text or "").lower()):
                    logger.info("[%s] ⚔️ Battle button disappeared; transition successful.", account_name)
                    return True

                # If battle button is still present, update current_btn reference for the next attempt
                new_btn = next((b for b in flattened_buttons if any(k in b.text.strip().lower() for k in ("battle", "battles", "fight"))), None)
                if new_btn:
                    current_btn = new_btn

            # Check 6: Check if a NEW message was sent to the chat that is a battle message
            try:
                recent_msgs = await client.get_messages(chat_id, limit=3)
                for r_msg in recent_msgs:
                    if r_msg.id != msg_id and self.battle_handler and self.battle_handler.is_battle_message(r_msg, account_name):
                        logger.info("[%s] ⚔️ New battle message detected in chat (msg_id %d)!", account_name, r_msg.id)
                        await self.battle_handler.handle_battle_turn(client, account_name, r_msg)
                        return True
            except Exception as e:
                logger.debug("[%s] Error checking recent messages: %s", account_name, e)

        logger.warning("[%s] ⚠️ Battle button clicked %d times but battle did not confirm started.", account_name, max_attempts)
        if self.on_catch_end:
            try:
                self.on_catch_end(account_name)
            except Exception as e:
                logger.error("[%s] Error releasing encounter lock: %s", account_name, e)
        return False

    async def handle_spawn(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """Selects the best available Pokéball button or enters battle based on category."""
        # Determine Hunt Category: 'kill' vs 'catch'
        bs = getattr(config, "BATTLE_SYSTEM", "").lower()
        if bs in ("hybrid", "catch"):
            hunt_mode = "catch"
        elif bs == "kill":
            hunt_mode = "kill"
        else:
            hm = getattr(config, "HUNT_MODE", "kill").lower()
            hunt_mode = "catch" if hm in ("hybrid", "catch") else "kill"

        if hunt_mode == "catch" and not config.AUTO_CATCH_ENABLED:
            logger.info("[%s] Auto-catch is disabled in config. Skipping encounter.", account_name)
            return False

        raw_text = message.raw_text or ""
        poke_name = self.extract_pokemon_name(raw_text)
        self.stats["encountered"] += 1

        if not message.buttons:
            logger.warning("[%s] No buttons attached to spawn message for %s.", account_name, poke_name)
            return False

        # Map available buttons
        button_map = {}
        for row in message.buttons:
            for btn in row:
                button_map[btn.text.strip().lower()] = btn

        logger.debug("[%s] Available spawn buttons: %s", account_name, list(button_map.keys()))

        # Check for star symbol '☆' indicating Pokémon was already caught / owned
        has_star = any(s in raw_text for s in ("☆", "★", "⭐", "🌟", "✨")) or "[☆" in raw_text
        if not has_star:
            has_star = any(any(s in label for s in ("☆", "★", "⭐", "🌟", "✨")) or "[☆" in label for label in button_map.keys())

        star_desc = " [☆ Star/Caught]" if has_star else " [New]"
        logger.info("[%s] 🌟 Wild encounter: %s%s | Hunt Category: %s",
                    account_name, poke_name, star_desc, hunt_mode.upper())

        # Find Battle button if present
        battle_btn = None
        for label, btn in button_map.items():
            clean = label.replace(" ", "")
            if any(k in clean for k in ("battle", "battles")) or clean == "fight" or "⚔️" in label:
                battle_btn = btn
                break

        # =========================================================================
        # CATEGORY 1: KILL MODE (FARMING PD/EXP -> MUST BATTLE!)
        # =========================================================================
        if hunt_mode == "kill" and battle_btn:
            logger.info("[%s] ⚔️ [HUNT: KILL] Encountered %s! Clicking Battle button to start fight...",
                        account_name, poke_name)
            return await self.click_battle_button_until_started(client, account_name, message, battle_btn)

        # =========================================================================
        # CATEGORY 2: CATCH MODE (THROWING POKÉBALLS)
        # =========================================================================
        chosen_button = None
        chosen_ball_name = None

        # CRITICAL USER RULE: If '☆' star is seen on wild encounter, USE REPEAT BALL!
        if has_star:
            repeat_btn = None
            for label, btn in button_map.items():
                clean = label.replace(" ", "")
                if "repeat" in clean:
                    repeat_btn = btn
                    break

            if repeat_btn:
                chosen_button = repeat_btn
                chosen_ball_name = "Repeat Ball"
                logger.info("[%s] 🔁 [☆ Star Detected] Wild encounter has '☆' -> Prioritizing Repeat Ball for %s!",
                            account_name, poke_name)
            else:
                logger.warning("[%s] 🔁 [☆ Star Detected] Wild %s has '☆' but Repeat Ball button is not available! Falling back to preferred balls.",
                               account_name, poke_name)

        # If not chosen yet (no star, or Repeat Ball was unavailable), check PREFERRED_BALLS
        if not chosen_button:
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

        # Fallback: check for any button mentioning ball, catch, or throw
        if not chosen_button:
            for label, btn in button_map.items():
                if "ball" in label or "catch" in label or "throw" in label:
                    chosen_button = btn
                    chosen_ball_name = btn.text.strip()
                    break

        # Fallback: if no ball buttons found but Battle button exists, start battle
        if not chosen_button and battle_btn:
            logger.info("[%s] No Pokéballs found on spawn encounter for %s. Clicking Battle button...",
                        account_name, poke_name)
            return await self.click_battle_button_until_started(client, account_name, message, battle_btn)

        # Fallback: default to the first button if it exists
        if not chosen_button and message.buttons:
            first_btn = message.buttons[0][0]
            first_text = first_btn.text.strip().lower()
            if any(k in first_text for k in ("battle", "fight")):
                return await self.click_battle_button_until_started(client, account_name, message, first_btn)
            logger.info("[%s] No standard ball found, defaulting to first button: '%s'", account_name, first_btn.text)
            chosen_button = first_btn
            chosen_ball_name = first_btn.text.strip()

        if chosen_button:
            if getattr(config, "FAST_BATTLE", True):
                delay = random.uniform(getattr(config, "FAST_BATTLE_MIN_DELAY", 0.05), getattr(config, "FAST_BATTLE_MAX_DELAY", 0.20))
            else:
                delay = random.uniform(1.2, 2.6)
            logger.info("[%s] ⚡ Throwing %s at %s in %.2fs...", account_name, chosen_ball_name, poke_name, delay)
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                await chosen_button.click()
                logger.info("[%s] Threw %s at %s!", account_name, chosen_ball_name, poke_name)
                return True
            except Exception as e:
                logger.error("[%s] Failed to click catch button: %s", account_name, e)
                return False

        logger.warning("[%s] Could not determine a catch or battle action for %s.", account_name, poke_name)
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
