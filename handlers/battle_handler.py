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
    "run", "pokemons", "pokemon", "switch", "bag", "items"
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

    def is_battle_message(self, message: Message) -> bool:
        """Determines if a message belongs to an active battle."""
        text = (message.raw_text or "").lower()
        if "battle begins" in text or "current turn:" in text:
            return True
        if "lower " in text and "'s hp" in text:
            return True
        if "hp " in text and "/" in text and ("scratch" in text or "power:" in text or message.buttons):
            return True
        return False

    def is_battle_end(self, message: Message) -> bool:
        """Checks if a battle concluded (enemy fainted or captured)."""
        text = (message.raw_text or "").lower()
        if "fainted" in text or "defeated" in text or "gained" in text and ("pd" in text or "exp" in text):
            return True
        if "congratulations" in text or "gotcha" in text or "was caught" in text:
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

        # Extract wild HP: "Lv. 8 • HP 27/27"
        hp_matches = re.findall(r"hp\s+(\d+)\s*/\s*(\d+)", text, re.IGNORECASE)
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

    def categorize_buttons(self, message: Message) -> Tuple[Dict[str, any], Dict[str, any], Dict[str, any]]:
        """
        Categorizes buttons into:
        1. move_buttons: moves known by current Pokémon (e.g. Scratch, Ember)
        2. ball_buttons: Pokéballs (e.g. Poke Ball, Repeat Ball, Ultra Ball)
        3. utility_buttons: Run, Pokemons, Bag, etc.
        """
        moves = {}
        balls = {}
        utilities = {}

        if not message.buttons:
            return moves, balls, utilities

        for row in message.buttons:
            for btn in row:
                label = btn.text.strip()
                label_clean = label.lower().replace(" ", "").replace("-", "")

                # Check if it's a utility button
                if any(u in label.lower() for u in UTILITY_BUTTON_NAMES):
                    utilities[label] = btn
                    continue

                # Check if it's a ball button (e.g. Poke Ball, Regular Ball, Repeat Ball)
                if "ball" in label.lower() or "regular" in label.lower():
                    balls[label] = btn
                    continue

                # Otherwise, it's a move button!
                moves[label] = btn

        return moves, balls, utilities

    async def handle_battle_turn(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """Executes a battle decision based on config.BATTLE_SYSTEM."""
        raw_text = message.raw_text or ""
        state = self.parse_battle_state(raw_text)
        moves, balls, utils = self.categorize_buttons(message)

        if not moves and not balls:
            logger.warning("[%s] No moves or ball buttons available in battle turn.", account_name)
            return False

        star_tag = " [☆ Caught]" if state["has_star"] else " [New]"
        logger.info("[%s] ⚔️ Battle Turn vs Wild %s%s (HP: %d/%d) | Moves: %s | Balls: %s",
                    account_name, state["wild_name"], star_tag,
                    state["wild_hp"], state["wild_max_hp"],
                    list(moves.keys()), list(balls.keys()))

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
                # Look specifically for Repeat Ball
                repeat_btn = None
                for label, btn in balls.items():
                    if "repeat" in label.lower():
                        repeat_btn = btn
                        break

                if repeat_btn:
                    chosen_button = repeat_btn
                    action_desc = f"[HYBRID] 🔁 Pokémon has '☆' -> Throwing Repeat Ball at {state['wild_name']}"
                elif balls:
                    # If Repeat Ball is not available, use regular available ball
                    chosen_button = list(balls.values())[0]
                    action_desc = f"[HYBRID] 🔁 Has '☆' but Repeat Ball missing -> Throwing {chosen_button.text}"
                elif moves:
                    # No balls, attack
                    ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                    chosen_button = moves[ranked[0][0]]
                    action_desc = f"[HYBRID] No balls left -> Attacking {state['wild_name']} with {ranked[0][0]}"

            # Rule 2: Not caught yet (No '☆')
            else:
                rarity = self.pokedex.get_rarity_tier(state["wild_name"])

                if rarity in ("legendary", "mythical", "rare", "starter"):
                    # High priority catch: Ultra/Master Ball
                    target_ball_btn = None
                    # Search preferred order: Master > Ultra > Great > Poke
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
                else:
                    # Common Pokémon
                    if config.HYBRID_KILL_COMMONS and moves:
                        # Kill common for PD
                        ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                        best_move, score = ranked[0]
                        chosen_button = moves[best_move]
                        action_desc = f"[HYBRID] Defeating common {state['wild_name']} with {best_move} for PD"
                    elif balls:
                        # Throw standard Regular Ball (or Poke Ball)
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

        if chosen_button:
            delay = random.uniform(1.2, 2.5)
            logger.info("[%s] %s (in %.2fs)...", account_name, action_desc, delay)
            await asyncio.sleep(delay)
            try:
                await chosen_button.click()
                return True
            except Exception as e:
                logger.error("[%s] Failed to click button '%s': %s", account_name, chosen_button.text, e)
                return False

        return False

    def handle_battle_result(self, account_name: str, message: Message):
        """Processes end of battle rewards and statistics."""
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

        elif "fainted" in text_lower or "defeated" in text_lower:
            self.stats["battles_won"] += 1
            logger.info("[%s] 💥 Wild Pokémon fainted! Battle won (Total: %d).",
                        account_name, self.stats["battles_won"])

        elif "caught" in text_lower or "congratulations" in text_lower:
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
