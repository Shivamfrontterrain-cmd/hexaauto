import asyncio
import logging
import random
import re
from typing import Dict, List, Optional, Tuple
from telethon import TelegramClient
from telethon.tl.custom.message import Message

import config
from core.pokedex import PokedexService

logger = logging.getLogger("autohexa.battle_handler")

# Standard utility buttons in HeXamonbot
UTILITY_BUTTON_NAMES = {
    "run", "pokemons", "pokemon", "switch", "bag", "items", "cancel", "back", "info"
}

# Sub-menu and turn progression action buttons
ACTION_BUTTON_NAMES = {
    "continue", "next", "proceed", "fight", "attack", "moves", "ok", "go", ">>"
}

BALL_BUTTON_NAMES = {
    "regular ball", "regularball", "regular",
    "poke ball", "pokeball", "repeat ball", "repeatball",
    "great ball", "greatball", "ultra ball", "ultraball",
    "master ball", "masterball", "safari ball", "safariball"
}

class BattleHandler:
    """Handles Pokémon battles for HeXamonbot with Kill/PD farming and Hybrid modes."""

    def __init__(self, pokedex: PokedexService, on_battle_end=None):
        self.pokedex = pokedex
        self.on_battle_end = on_battle_end
        self.active_battles: Dict[str, dict] = {}
        self.stats = {
            "battles_won": 0,
            "pd_earned": 0,
            "caught_repeat": 0,
            "caught_regular": 0
        }

    def is_battle_end(self, message: Message) -> bool:
        """Checks if a battle concluded (enemy fainted or captured)."""
        if not message:
            return False
        text = (message.raw_text or "").lower()

        # Faint / defeat / victory indicators
        if any(w in text for w in (
            "fainted", "faint", "defeated", "defeat", "knocked out", "k.o.", " ko!", "ko.",
            "won the battle", "battle won", "you won", "victory"
        )):
            return True

        # Pokédollars or EXP gained
        if any(w in text for w in ("gained", "earned", "received")) and any(r in text for r in ("pd", "exp", "pokedollars", "pokédollars", "experience")):
            return True

        # Catch outcomes
        if any(w in text for w in ("congratulations", "gotcha", "was caught", "caught", "captured")):
            return True

        # Escapes or player blackout
        if any(w in text for w in ("ran away", "escaped", "fled", "flew away", "blacked out", "whited out", "lost the battle")):
            return True

        # Wild HP explicitly zero
        if re.search(r"hp[:\s]*0\s*/", text, re.IGNORECASE):
            return True

        return False

    def is_battle_message(self, message: Message, account_name: Optional[str] = None) -> bool:
        """Determines if a message belongs to an active battle."""
        if not message:
            return False

        # If it's already concluded, it's an end message, not an active battle turn
        if self.is_battle_end(message):
            return False

        text = (message.raw_text or "").lower()
        buttons = message.buttons or []

        # 1. Button-based detection (most reliable indicator)
        if buttons:
            button_texts = [btn.text.strip().lower() for row in buttons for btn in row]

            # Battle utility buttons (Run, Pokemons, Switch, Bag)
            if any(any(u in b for u in UTILITY_BUTTON_NAMES) for b in button_texts):
                return True

            # Action / sub-menu buttons (Fight, Attack, Moves, Continue, Next)
            if any(any(a in b for a in ACTION_BUTTON_NAMES) for b in button_texts):
                return True

            # Check if any button matches a known Pokémon move
            for b_text in button_texts:
                clean_name = self.pokedex.clean_name(b_text)
                if self.pokedex.get_move(clean_name):
                    return True

            # Check if buttons contain both Pokéballs and other choices in a wild/battle context
            has_ball = any("ball" in b for b in button_texts)
            if has_ball and len(button_texts) > 1 and any(w in text for w in ("hp", "lv", "turn", "wild", "vs")):
                return True

        # 2. Account is actively in a tracked battle
        if account_name and account_name in self.active_battles and buttons:
            return True

        # 3. Text-based battle indicators
        battle_phrases = (
            "battle begins", "battle started", "current turn:", "turn:", "your turn",
            "lower ", "'s hp", "for a better chance of catching"
        )
        if any(phrase in text for phrase in battle_phrases):
            return True

        # HP pattern (e.g., "HP 27/27", "HP: 13/27", "13/27") with battle context
        if re.search(r"hp[:\s]*\d+\s*/\s*\d+", text, re.IGNORECASE):
            return True

        if buttons and ("wild" in text or "vs" in text) and any(w in text for w in ("lv", "level", "hp", "attack", "damage", "scratch", "power:")):
            return True

        return False

    def parse_battle_state(self, text: str) -> dict:
        """Extracts wild Pokémon name, star symbol, HP, and player info from battle text."""
        state = {
            "wild_name": "Unknown",
            "has_star": False,
            "wild_types": [],
            "wild_hp": 1,
            "wild_max_hp": 1,
            "my_name": "Unknown",
            "my_types": [],
            "my_hp": 1,
            "my_max_hp": 1,
        }

        # Check for star symbol '☆' (indicates already caught once)
        if "☆" in text:
            state["has_star"] = True

        # Extract wild Pokémon: "Wild Metapod [Bug]" or "Wild Metapod ☆ [Bug]"
        wild_match = re.search(r"wild\s+([A-Za-z0-9\-]+)(?:\s*☆)?\s*(?:\[(.*?)\])?", text, re.IGNORECASE)
        if wild_match:
            state["wild_name"] = wild_match.group(1).title()
            if wild_match.group(2):
                state["wild_types"] = [t.strip().lower() for t in wild_match.group(2).split("/")]

        # Extract wild HP: "Lv. 8 • HP 27/27" or "HP: 13/27"
        hp_matches = re.findall(r"hp[:\s]*(\d+)\s*/\s*(\d+)", text, re.IGNORECASE)
        if hp_matches:
            # First match is usually wild Pokémon
            state["wild_hp"] = int(hp_matches[0][0])
            state["wild_max_hp"] = int(hp_matches[0][1])
            if len(hp_matches) > 1:
                # Second match is player's Pokémon
                state["my_hp"] = int(hp_matches[1][0])
                state["my_max_hp"] = int(hp_matches[1][1])

        # Extract active player Pokémon: "Current turn: Shivam\nCharmander [Fire]"
        turn_match = re.search(r"current turn:.*?\n([A-Za-z0-9\-]+)\s*(?:\[(.*?)\])?", text, re.IGNORECASE)
        if turn_match:
            state["my_name"] = turn_match.group(1).title()
            if turn_match.group(2):
                state["my_types"] = [t.strip().lower() for t in turn_match.group(2).split("/")]

        # If types were not in message text, look them up in pokedex.json
        if not state["wild_types"]:
            poke_data = self.pokedex.get_pokemon(state["wild_name"])
            if poke_data:
                state["wild_types"] = poke_data.get("types", [])

        if not state["my_types"]:
            poke_data = self.pokedex.get_pokemon(state["my_name"])
            if poke_data:
                state["my_types"] = poke_data.get("types", [])

        return state

    def categorize_buttons(self, message: Message) -> Tuple[Dict[str, any], Dict[str, any], Dict[str, any], Dict[str, any]]:
        """
        Categorizes buttons into:
        1. moves: Attack moves (e.g. Scratch, Ember, Water Gun)
        2. balls: Pokéballs (e.g. Poke Ball, Repeat Ball)
        3. utilities: Run, Pokemons, Bag, Switch
        4. actions: Continue, Next, Fight, Attack, Proceed
        """
        moves = {}
        balls = {}
        utilities = {}
        actions = {}

        if not message.buttons:
            return moves, balls, utilities, actions

        for row in message.buttons:
            for btn in row:
                label = btn.text.strip()
                label_lower = label.lower()

                # Check if it's a utility button
                if any(u in label_lower for u in UTILITY_BUTTON_NAMES):
                    utilities[label] = btn
                    continue

                # Check if it's an action/continue/fight button
                if any(a in label_lower for a in ACTION_BUTTON_NAMES):
                    actions[label] = btn
                    continue

                # Check if it's a ball button
                if "ball" in label_lower or "regular" in label_lower:
                    balls[label] = btn
                    continue

                # Otherwise, it's a move button!
                moves[label] = btn

        return moves, balls, utilities, actions

    def _schedule_battle_watchdog(self, client: TelegramClient, account_name: str, chat_id: any, msg_id: int):
        """Schedules a safety watchdog to re-check the battle message if Telegram edit event was missed."""
        async def watchdog():
            await asyncio.sleep(2.8)
            battle = self.active_battles.get(account_name)
            if not battle or battle.get("msg_id") != msg_id:
                return

            now = asyncio.get_event_loop().time()
            if now - battle.get("last_click", 0) < 2.5:
                return

            try:
                latest_msg = await client.get_messages(chat_id, ids=msg_id)
                if not latest_msg:
                    return

                if self.is_battle_end(latest_msg):
                    logger.info("[%s] ⚡ Watchdog detected battle victory/end on msg_id %d.", account_name, msg_id)
                    self.handle_battle_result(account_name, latest_msg)
                elif self.is_battle_message(latest_msg, account_name) and latest_msg.buttons:
                    logger.info("[%s] 🔄 Watchdog re-triggering attack on active turn (msg_id %d)...", account_name, msg_id)
                    await self.handle_battle_turn(client, account_name, latest_msg)
            except Exception as e:
                logger.debug("[%s] Watchdog check error: %s", account_name, e)

        asyncio.create_task(watchdog())

    async def handle_battle_turn(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """Executes a battle decision based on config.BATTLE_SYSTEM."""
        raw_text = message.raw_text or ""
        state = self.parse_battle_state(raw_text)
        moves, balls, utils, actions = self.categorize_buttons(message)

        if not moves and not balls and not actions:
            logger.warning("[%s] No moves, ball buttons, or action buttons available in battle turn.", account_name)
            return False

        star_tag = " [☆ Caught]" if state["has_star"] else " [New]"
        logger.info("[%s] ⚔️ Battle Turn vs Wild %s%s (HP: %d/%d) | Moves: %s | Actions: %s | Balls: %s",
                    account_name, state["wild_name"], star_tag,
                    state["wild_hp"], state["wild_max_hp"],
                    list(moves.keys()), list(actions.keys()), list(balls.keys()))

        chosen_button = None
        action_desc = ""

        # =========================================================================
        # SYSTEM 1: KILL MODE (FARMING PD - POKÉDOLLARS)
        # =========================================================================
        if config.BATTLE_SYSTEM == "kill":
            if moves:
                ranked = self.pokedex.rank_moves_for_damage(
                    list(moves.keys()), state["my_types"], state["wild_types"]
                )
                best_move_name, score = ranked[0]
                chosen_button = moves[best_move_name]
                action_desc = f"[KILL MODE] Attacking with {best_move_name} (Damage score: {score:.1f})"
            elif actions:
                # If there are continue/next or fight buttons
                continue_btn = next((btn for label, btn in actions.items() if any(c in label.lower() for c in ("continue", "next", "proceed", ">>", "ok"))), None)
                fight_btn = next((btn for label, btn in actions.items() if any(f in label.lower() for f in ("fight", "attack", "moves"))), None)
                chosen_button = continue_btn or fight_btn or list(actions.values())[0]
                action_desc = f"[KILL MODE] Advancing battle turn with '{chosen_button.text}'"
            elif balls:
                # Fallback if no moves are left (e.g. Struggle or out of PP)
                chosen_button = list(balls.values())[0]
                action_desc = "[KILL MODE] No moves available, throwing ball fallback"

        # =========================================================================
        # SYSTEM 2: HYBRID BY RARITY MODE
        # =========================================================================
        else:
            # Rule 1: '☆' detected -> Pokémon was already caught once -> USE REPEAT BALL!
            if state["has_star"]:
                repeat_btn = None
                for label, btn in balls.items():
                    if "repeat" in label.lower():
                        repeat_btn = btn
                        break

                if repeat_btn:
                    chosen_button = repeat_btn
                    action_desc = f"[HYBRID] 🔁 Pokémon has '☆' -> Throwing Repeat Ball at {state['wild_name']}"
                elif balls:
                    chosen_button = list(balls.values())[0]
                    action_desc = f"[HYBRID] 🔁 Has '☆' but Repeat Ball missing -> Throwing {chosen_button.text}"
                elif moves:
                    ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                    chosen_button = moves[ranked[0][0]]
                    action_desc = f"[HYBRID] No balls left -> Attacking {state['wild_name']} with {ranked[0][0]}"
                elif actions:
                    chosen_button = list(actions.values())[0]
                    action_desc = f"[HYBRID] Advancing turn with '{chosen_button.text}'"

            # Rule 2: Not caught yet (No '☆')
            else:
                rarity = self.pokedex.get_rarity_tier(state["wild_name"])

                if rarity in ("legendary", "mythical", "rare", "starter"):
                    # High priority catch: Ultra/Master Ball
                    target_ball_btn = None
                    for pref in ["master", "ultra", "great", "poke"]:
                        for label, btn in balls.items():
                            if pref in label.lower():
                                target_ball_btn = btn
                                break
                        if target_ball_btn:
                            break

                    if target_ball_btn:
                        chosen_button = target_ball_btn
                        action_desc = f"[HYBRID] 🌟 {rarity.upper()} encounter -> Throwing {target_ball_btn.text} at {state['wild_name']}"
                    elif moves:
                        # Weaken carefully with safe move
                        safe_move, score = self.pokedex.find_safe_move_to_weaken(
                            list(moves.keys()), state["my_types"], state["wild_types"]
                        )
                        chosen_button = moves[safe_move]
                        action_desc = f"[HYBRID] Weakening {rarity} {state['wild_name']} with safe move {safe_move}"
                    elif actions:
                        chosen_button = list(actions.values())[0]
                        action_desc = f"[HYBRID] Advancing turn with '{chosen_button.text}'"
                else:
                    # Common Pokémon
                    if config.HYBRID_KILL_COMMONS and moves:
                        ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                        best_move, score = ranked[0]
                        chosen_button = moves[best_move]
                        action_desc = f"[HYBRID] Defeating common {state['wild_name']} with {best_move} for PD"
                    elif balls:
                        regular_btn = None
                        for pref in ["regular", "poke"]:
                            for label, btn in balls.items():
                                if pref in label.lower():
                                    regular_btn = btn
                                    break
                            if regular_btn:
                                break
                        chosen_button = regular_btn or list(balls.values())[0]
                        action_desc = f"[HYBRID] Throwing {chosen_button.text} (Regular Ball) at common {state['wild_name']}"
                    elif moves:
                        ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                        chosen_button = moves[ranked[0][0]]
                        action_desc = f"[HYBRID] Out of balls -> Attacking {state['wild_name']} with {ranked[0][0]}"
                    elif actions:
                        chosen_button = list(actions.values())[0]
                        action_desc = f"[HYBRID] Advancing turn with '{chosen_button.text}'"

        if chosen_button:
            if getattr(config, "FAST_BATTLE", True):
                delay = random.uniform(getattr(config, "FAST_BATTLE_MIN_DELAY", 0.05), getattr(config, "FAST_BATTLE_MAX_DELAY", 0.20))
            else:
                delay = random.uniform(1.2, 2.5)

            logger.info("[%s] ⚡ %s (in %.2fs)...", account_name, action_desc, delay)
            if delay > 0:
                await asyncio.sleep(delay)

            # Record active battle state to maintain continuity across multiple turns
            self.active_battles[account_name] = {
                "msg_id": message.id,
                "chat_id": message.chat_id,
                "wild_name": state["wild_name"],
                "last_click": asyncio.get_event_loop().time()
            }

            try:
                # 3.0s timeout ensures we don't hang if Telegram bot omits the callback query answer
                await asyncio.wait_for(chosen_button.click(), timeout=3.0)
                click_success = True
            except asyncio.TimeoutError:
                logger.debug("[%s] Callback query answer timed out (click request was dispatched).", account_name)
                click_success = True
            except Exception as e:
                logger.error("[%s] Failed to click button '%s': %s", account_name, chosen_button.text, e)
                click_success = False

            # Arm watchdog to guarantee consecutive turns keep clicking until Pokémon dies
            self._schedule_battle_watchdog(client, account_name, message.chat_id, message.id)
            return click_success

        return False

    def handle_battle_result(self, account_name: str, message: Message):
        """Processes end of battle rewards and statistics."""
        # Clear from active battles
        self.active_battles.pop(account_name, None)

        text = message.raw_text or ""
        text_lower = text.lower()

        # Check for PD (Pokédollars) earned
        pd_match = re.search(r"(\d+(?:,\d+)?)\s*(?:pd|pokédollars|pokedollars)", text_lower)
        if pd_match:
            pd_amount = int(pd_match.group(1).replace(",", ""))
            self.stats["pd_earned"] += pd_amount
            self.stats["battles_won"] += 1
            logger.info("[%s] 💰 BATTLE VICTORY! Earned %d PD! (Total Won: %d, Total PD: %d)",
                        account_name, pd_amount, self.stats["battles_won"], self.stats["pd_earned"])

        elif "fainted" in text_lower or "defeated" in text_lower or "won" in text_lower or "victory" in text_lower:
            self.stats["battles_won"] += 1
            logger.info("[%s] 💥 Wild Pokémon fainted/defeated! Battle won (Total: %d).",
                        account_name, self.stats["battles_won"])

        elif "caught" in text_lower or "congratulations" in text_lower or "gotcha" in text_lower:
            if "repeat" in text_lower or "☆" in text:
                self.stats["caught_repeat"] += 1
                logger.info("[%s] 🔁 Caught Pokémon with Repeat Ball! Total Repeat: %d",
                            account_name, self.stats["caught_repeat"])
            else:
                self.stats["caught_regular"] += 1
                logger.info("[%s] 🎉 Caught new Pokémon! Total: %d",
                            account_name, self.stats["caught_regular"])

        # Signal that the battle has fully completed so AutoHunter can proceed to next hunt
        if self.on_battle_end:
            try:
                self.on_battle_end(account_name)
            except Exception as e:
                logger.error("[%s] Error in on_battle_end callback: %s", account_name, e)
