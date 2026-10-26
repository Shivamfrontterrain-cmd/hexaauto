import asyncio
import logging
import random
import re
from typing import Dict, Optional
from telethon import TelegramClient
from telethon.tl.custom.message import Message

import config
from core.pokedex import is_pokeball_button, is_pokeball_menu_button, check_has_star

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

    def __init__(self, on_catch_end=None, battle_handler=None, hunt_mode: Optional[str] = None):
        self.on_catch_end = on_catch_end
        self.battle_handler = battle_handler
        self.hunt_mode = hunt_mode
        self.account_modes: Dict[str, str] = {}
        if battle_handler and hasattr(battle_handler, "starred_encounters"):
            self.starred_encounters = battle_handler.starred_encounters
        else:
            self.starred_encounters = {}
        self.active_encounters: Dict[str, dict] = {}
        self.stats = {
            "encountered": 0,
            "caught": 0,
            "fled": 0
        }

    def set_hunt_mode(self, mode: str, account_name: Optional[str] = None):
        """Sets the hunt strategy dynamically per instance or per account ('catch' vs 'kill')."""
        if account_name:
            self.account_modes[account_name] = mode
        else:
            self.hunt_mode = mode

    def get_hunt_mode(self, account_name: Optional[str] = None) -> str:
        """Returns the active hunt strategy for the specified account."""
        if account_name and account_name in self.account_modes:
            return self.account_modes[account_name]
        if self.hunt_mode:
            return self.hunt_mode
        bs = getattr(config, "BATTLE_SYSTEM", "").lower()
        if bs in ("hybrid", "catch"):
            return "catch"
        elif bs == "kill":
            return "kill"
        hm = getattr(config, "HUNT_MODE", "kill").lower()
        return "catch" if hm in ("hybrid", "catch") else "kill"

    def _trigger_catch_end(self, account_name: str, is_catch: bool = False):
        """Safely dispatches on_catch_end callback with is_catch outcome flag."""
        if self.on_catch_end:
            try:
                import inspect
                sig = inspect.signature(self.on_catch_end)
                if "is_catch" in sig.parameters:
                    self.on_catch_end(account_name, is_catch=is_catch)
                else:
                    self.on_catch_end(account_name)
            except Exception as e:
                logger.error("[%s] Error in on_catch_end callback: %s", account_name, e)

    def is_spawn_message(self, message: Message) -> bool:
        """Checks if the message represents a wild Pokémon encounter outside of battle."""
        if not message:
            return False

        text = (message.raw_text or "").lower()

        # If it's a catch result, switch prompt, or battle message, do not treat as a fresh spawn
        if self.battle_handler and self.battle_handler.is_switch_prompt(message):
            return False
        if self.is_catch_result(message):
            return False
        if any(b in text for b in ("battle begins", "battle started", "current turn:", "turn:")):
            return False
        if re.search(r"hp[:\s]*\d+\s*/\s*\d+", text, re.IGNORECASE):
            return False

        # If buttons contain battle utility or action buttons, it belongs to BattleHandler!
        buttons = message.buttons or []
        if buttons:
            button_texts = [btn.text.strip().lower() for row in buttons for btn in row]
            # If buttons contain actual battle attack moves (Scratch, Tackle, etc.)
            if self.battle_handler and any(self.battle_handler.pokedex.get_move(self.battle_handler.pokedex.clean_name(b)) for b in button_texts):
                return False
            battle_btn_keywords = (
                "switch", "fight", "attack", "moves", "continue"
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
        if not message:
            return False
        # Active battle messages and switch prompts are battle turns, not catch results!
        if self.battle_handler and (self.battle_handler.is_battle_message(message) or self.battle_handler.is_switch_prompt(message)):
            return False
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
                has_ball_buttons = any(is_pokeball_button(b, self.battle_handler.pokedex if self.battle_handler else None) for b in button_texts)

                # Has battle concluded or started?
                if self.battle_handler:
                    if self.battle_handler.is_battle_end(latest_msg):
                        logger.info("[%s] ⚔️ Battle ended immediately on msg_id %d!", account_name, msg_id)
                        self.active_encounters.pop(account_name, None)
                        self.battle_handler.handle_battle_result(account_name, latest_msg)
                        return True
                    if self.battle_handler.is_battle_message(latest_msg, account_name) or has_battle_phrase or has_hp_indicator or has_battle_controls or has_ball_buttons:
                        logger.info("[%s] ⚔️ Battle confirmed started on msg_id %d (attempt %d)!", account_name, msg_id, attempt)
                        self.active_encounters.pop(account_name, None)
                        await self.battle_handler.handle_battle_turn(client, account_name, latest_msg)
                        return True

                # If battle button is no longer present on the message, it transitioned!
                if not has_battle_btn and (latest_buttons or latest_text != (message.raw_text or "").lower()):
                    logger.info("[%s] ⚔️ Battle button disappeared; transition successful.", account_name)
                    self.active_encounters.pop(account_name, None)
                    if self.battle_handler:
                        await self.battle_handler.handle_battle_turn(client, account_name, latest_msg)
                    return True

                # If battle button is still present, update current_btn reference for the next attempt
                new_btn = next((b for b in flattened_buttons if any(k in b.text.strip().lower() for k in ("battle", "battles", "fight"))), None)
                if new_btn:
                    current_btn = new_btn

            # Check 6: If original message couldn't be re-fetched (e.g. deleted), check if a NEW message was sent
            if latest_msg is None:
                try:
                    recent_msgs = await client.get_messages(chat_id, limit=3)
                    for r_msg in recent_msgs:
                        if self.battle_handler and self.battle_handler.is_battle_message(r_msg, account_name):
                            logger.info("[%s] ⚔️ New battle message detected in chat (msg_id %d)!", account_name, r_msg.id)
                            self.active_encounters.pop(account_name, None)
                            await self.battle_handler.handle_battle_turn(client, account_name, r_msg)
                            return True
                except Exception as e:
                    logger.debug("[%s] Error checking recent messages: %s", account_name, e)

        logger.warning("[%s] ⚠️ Battle button clicked %d times but battle did not confirm started.", account_name, max_attempts)
        self.active_encounters.pop(account_name, None)
        self._trigger_catch_end(account_name, is_catch=False)
        return False

    async def handle_spawn(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """Selects the best available Pokéball button or enters battle based on category."""
        # Determine Hunt Category: 'kill' vs 'catch'
        hunt_mode = self.get_hunt_mode(account_name)

        raw_text = message.raw_text or ""
        poke_name = self.extract_pokemon_name(raw_text)
        self.stats["encountered"] += 1

        # Track active encounter so AutoHunter holds the next hunt
        self.active_encounters[account_name] = {
            "msg_id": message.id,
            "chat_id": message.chat_id,
            "poke_name": poke_name,
            "time": asyncio.get_event_loop().time()
        }

        if hunt_mode == "catch" and not config.AUTO_CATCH_ENABLED:
            logger.info("[%s] Auto-catch is disabled in config. Skipping encounter.", account_name)
            self.active_encounters.pop(account_name, None)
            self._trigger_catch_end(account_name, is_catch=False)
            return False

        if not message.buttons:
            logger.warning("[%s] No buttons attached to spawn message for %s.", account_name, poke_name)
            self.active_encounters.pop(account_name, None)
            self._trigger_catch_end(account_name, is_catch=False)
            return False

        # Map available buttons
        button_map = {}
        for row in message.buttons:
            for btn in row:
                button_map[btn.text.strip().lower()] = btn

        logger.debug("[%s] Available spawn buttons: %s", account_name, list(button_map.keys()))

        # Check for star symbol '☆' indicating Pokémon was already caught / owned (unicode or custom emoji)
        has_star = check_has_star(message, raw_text)
        if not has_star:
            has_star = any(check_has_star(text=label) for label in button_map.keys())

        # If already tracked as starred for this active encounter (e.g. from previous edit/turn or break free), retain it!
        if self.starred_encounters.get(account_name, False):
            has_star = True

        # Track star encounter in shared dict so BattleHandler knows this encounter has '☆'
        if has_star:
            self.starred_encounters[account_name] = True
            logger.info("[%s] ⭐ Wild encounter %s has '☆' star! Tracked for Repeat Ball catch.", account_name, poke_name)
        else:
            self.starred_encounters[account_name] = False

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

        # Check if any ball buttons are available directly on this message
        has_ball_buttons = any(is_pokeball_button(l, self.battle_handler.pokedex if self.battle_handler else None) for l in button_map.keys())

        # =========================================================================
        # CASE 1: BATTLE BUTTON PRESENT AND NO BALLS ON SCREEN (e.g. Clefairy spawn)
        # MUST CLICK BATTLE TO START ENCOUNTER/BATTLE (IN BOTH KILL AND CATCH MODES!)
        # =========================================================================
        if battle_btn and not has_ball_buttons:
            logger.info("[%s] ⚔️ Wild encounter %s%s has Battle button (no balls on screen). Clicking Battle until started...",
                        account_name, poke_name, star_desc)
            return await self.click_battle_button_until_started(client, account_name, message, battle_btn)

        # =========================================================================
        # CASE 2: KILL MODE (FARMING PD/EXP -> MUST BATTLE!)
        # =========================================================================
        if hunt_mode == "kill" and battle_btn and not has_star:
            logger.info("[%s] ⚔️ [HUNT: KILL] Encountered %s! Clicking Battle button to start fight...",
                        account_name, poke_name)
            return await self.click_battle_button_until_started(client, account_name, message, battle_btn)

        # =========================================================================
        # CASE 3: CATCH MODE (THROWING POKÉBALLS IF PRESENT DIRECTLY)
        # =========================================================================
        chosen_button = None
        chosen_ball_name = None

        # CRITICAL USER RULE: If '☆' star is seen on wild encounter, USE REPEAT BALL!
        if has_star:
            repeat_btn = None
            for label, btn in button_map.items():
                clean = label.replace(" ", "").replace("-", "")
                if "repeat" in clean:
                    repeat_btn = btn
                    break

            if repeat_btn:
                chosen_button = repeat_btn
                chosen_ball_name = "Repeat Ball"
                logger.info("[%s] 🔁 [☆ Star Detected] Wild encounter has '☆' -> Prioritizing Repeat Ball for %s!",
                            account_name, poke_name)
            else:
                # Check for Pokéball menu button (e.g. 'Pokeballs', 'Balls', 'Bag')
                pokeballs_menu_btn = None
                for label, btn in button_map.items():
                    if is_pokeball_menu_button(label) and not any(k in label.lower() for k in ("bag", "backpack")):
                        pokeballs_menu_btn = btn
                        break
                if not pokeballs_menu_btn:
                    for label, btn in button_map.items():
                        if any(k in label.lower() for k in ("bag", "backpack")) or "🎒" in label:
                            pokeballs_menu_btn = btn
                            break
                if not pokeballs_menu_btn:
                    for label, btn in button_map.items():
                        if "item" in label.lower():
                            pokeballs_menu_btn = btn
                            break

                if pokeballs_menu_btn:
                    chosen_button = pokeballs_menu_btn
                    chosen_ball_name = f"{pokeballs_menu_btn.text} (for Repeat Ball)"
                    logger.info("[%s] 🔁 [☆ Star Detected] Wild %s has '☆' -> Opening %s to access Repeat Ball!",
                                account_name, poke_name, pokeballs_menu_btn.text)
                else:
                    logger.warning("[%s] 🔁 [☆ Star Detected] Wild %s has '☆' but Repeat Ball button is not available! Falling back to preferred balls.",
                                   account_name, poke_name)

        # If not chosen yet (no star, or Repeat Ball was unavailable), check PREFERRED_BALLS
        if not chosen_button:
            for pref in config.PREFERRED_BALLS:
                pref_lower = pref.lower().replace(" ", "")
                for label, btn in button_map.items():
                    clean_label = label.replace(" ", "")
                    if pref_lower in clean_label and is_pokeball_button(label, self.battle_handler.pokedex if self.battle_handler else None):
                        chosen_button = btn
                        chosen_ball_name = pref
                        break
                if chosen_button:
                    break

        # Fallback: check for any button that is a valid Pokéball, catch, or throw
        if not chosen_button:
            for label, btn in button_map.items():
                if is_pokeball_button(label, self.battle_handler.pokedex if self.battle_handler else None) or "catch" in label or "throw" in label:
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
                self.active_encounters.pop(account_name, None)
                self._trigger_catch_end(account_name, is_catch=False)
                return False

        logger.warning("[%s] Could not determine a catch or battle action for %s.", account_name, poke_name)
        self.active_encounters.pop(account_name, None)
        self._trigger_catch_end(account_name, is_catch=False)
        return False

    def handle_catch_result(self, account_name: str, message: Message, client: Optional[TelegramClient] = None):
        """Processes the outcome of a catch attempt and updates statistics."""
        text = (message.raw_text or "").lower()
        has_buttons = bool(message.buttons)

        is_catch = any(w in text for w in ("caught", "congratulations", "gotcha", "was caught", "captured"))
        is_fled = any(w in text for w in ("fled", "ran away", "flew away", "vanished", "escaped and ran away", "got away"))

        # 1. Successful Catch
        if is_catch:
            self.stats["caught"] += 1
            logger.info("[%s] 🎉 CATCH SUCCESS! Total caught: %d/%d",
                        account_name, self.stats["caught"], self.stats["encountered"])
            self.active_encounters.pop(account_name, None)
            self.starred_encounters.pop(account_name, None)
            if self.battle_handler and hasattr(self.battle_handler, "active_battles"):
                self.battle_handler.active_battles.pop(account_name, None)
            self._trigger_catch_end(account_name, is_catch=True)
            return

        # 2. Pokémon Fled / Ran away
        if is_fled:
            self.stats["fled"] += 1
            logger.info("[%s] 💨 Pokémon fled from encounter. Total fled: %d",
                        account_name, self.stats["fled"])
            self.active_encounters.pop(account_name, None)
            self.starred_encounters.pop(account_name, None)
            if self.battle_handler and hasattr(self.battle_handler, "active_battles"):
                self.battle_handler.active_battles.pop(account_name, None)
            self._trigger_catch_end(account_name, is_catch=False)
            return

        # 3. Pokémon broke free but has NOT fled (Encounter / battle is still active!)
        if "broke free" in text or "escaped" in text:
            logger.info("[%s] 💨 Pokémon broke free but has not fled. Holding encounter lock...", account_name)

            # If message transitioned into active battle with battle controls
            if self.battle_handler and self.battle_handler.is_battle_message(message, account_name):
                logger.info("[%s] ⚔️ Encounter transitioned to battle after break free. Routing to BattleHandler...", account_name)
                self.active_encounters.pop(account_name, None)
                if client:
                    asyncio.create_task(self.battle_handler.handle_battle_turn(client, account_name, message))
                return

            # If spawn catch buttons are still present
            if has_buttons and client is not None:
                logger.info("[%s] 💨 Re-attempting catch after breaking free...", account_name)
                asyncio.create_task(self.handle_spawn(client, account_name, message))
                return

            # No buttons attached yet (e.g. intermediate text update) -> DO NOT trigger catch end!
            logger.info("[%s] ⏳ Waiting for turn/battle controls to update after breaking free...", account_name)
            return
