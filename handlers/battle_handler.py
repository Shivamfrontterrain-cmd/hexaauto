import asyncio
import logging
import random
import re
from typing import Dict, List, Optional, Tuple
from telethon import TelegramClient
from telethon.tl.custom.message import Message

import config
from core.pokedex import PokedexService, is_pokeball_button, is_pokeball_menu_button, check_has_star

logger = logging.getLogger("autohexa.battle_handler")

# Standard utility buttons in HeXamonbot
# Standard battle utility buttons (Run, Pokemons, Switch, Bag, Pokeballs)
BATTLE_UTILITY_BUTTONS = {
    "run", "pokemons", "pokemon", "switch", "bag", "items", "item", "cancel", "back",
    "pokeballs", "pokéballs", "poke balls", "poké balls", "balls", "throw ball"
}

# Info and helper buttons (EV Yield, Pokedex, Info, Guide)
INFO_BUTTON_NAMES = {
    "ev yield", "ev", "yield", "pokedex", "dex", "info", "guide", "stats"
}

UTILITY_BUTTON_NAMES = BATTLE_UTILITY_BUTTONS | INFO_BUTTON_NAMES

# Sub-menu and turn progression action buttons inside battles
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

    def __init__(
        self,
        pokedex: PokedexService,
        on_battle_end=None,
        spawn_handler=None,
        hunt_mode: Optional[str] = None,
        auto_switch: Optional[bool] = None
    ):
        self.pokedex = pokedex
        self.on_battle_end = on_battle_end
        self.spawn_handler = spawn_handler
        self.hunt_mode = hunt_mode
        self.auto_switch = getattr(config, "AUTO_SWITCH_ENABLED", True) if auto_switch is None else auto_switch
        self.account_modes: Dict[str, str] = {}
        self.account_auto_switch: Dict[str, bool] = {}
        self.active_battles: Dict[str, dict] = {}
        self.starred_encounters: Dict[str, bool] = {}
        self.bag_opened: Dict[str, int] = {}
        self.stats = {
            "battles_won": 0,
            "pd_earned": 0,
            "caught_repeat": 0,
            "caught_regular": 0
        }

    def set_hunt_mode(self, mode: str, account_name: Optional[str] = None):
        """Sets the hunt/battle mode dynamically per instance or per account."""
        if account_name:
            self.account_modes[account_name] = mode
        else:
            self.hunt_mode = mode

    def get_hunt_mode(self, account_name: Optional[str] = None) -> str:
        """Resolves active hunt mode: account override -> instance override -> config fallback."""
        if account_name and account_name in self.account_modes:
            mode = self.account_modes[account_name].lower().strip()
            return "catch" if mode in ("catch", "hybrid") else "kill"
        if self.hunt_mode:
            mode = self.hunt_mode.lower().strip()
            return "catch" if mode in ("catch", "hybrid") else "kill"
        bs = getattr(config, "BATTLE_SYSTEM", "").lower().strip()
        if bs in ("hybrid", "catch"):
            return "catch"
        elif bs == "kill":
            return "kill"
        hm = getattr(config, "HUNT_MODE", "kill").lower().strip()
        return "catch" if hm in ("hybrid", "catch") else "kill"

    def set_auto_switch(self, enabled: bool, account_name: Optional[str] = None):
        """Enables or disables auto-switch dynamically per account or instance."""
        if account_name:
            self.account_auto_switch[account_name] = enabled
        else:
            self.auto_switch = enabled

    def get_auto_switch(self, account_name: Optional[str] = None) -> bool:
        """Checks if auto-switch is enabled for the account or instance."""
        if account_name and account_name in self.account_auto_switch:
            return self.account_auto_switch[account_name]
        return self.auto_switch

    def is_switch_prompt(self, message: Message) -> bool:
        """Determines if a battle message is prompting the player to switch Pokémon."""
        if not message:
            return False
        raw = message.raw_text or ""
        clean = raw.lower().replace("é", "e").replace("’", "'").replace("`", "'")
        clean = " ".join(clean.split())

        # 1. Direct prompt phrases
        switch_phrases = (
            "choose a pokemon to switch to",
            "choose a pokemon to switch",
            "choose a pokemon to send out",
            "choose which pokemon to send out",
            "select a pokemon to switch",
            "select a pokemon to send out",
            "which pokemon will you switch",
            "choose your next pokemon",
            "choose a pokemon to replace",
            "switch to:",
            "send out:"
        )
        if any(p in clean for p in switch_phrases):
            return True

        # 2. Check for faint followed by switch selection or party listing
        if ("fainted" in clean or "faint" in clean) and any(w in clean for w in ("switch", "send out", "choose", "select")):
            return True

        # 3. Party listing in text (e.g. "1. Metagross (Fainted)", "2. Delphox")
        has_party_list = bool(re.search(r"(?:^|[^\d])[1-6][\.\:\)\-]\s+[A-Za-z]", raw))

        # 4. Button heuristic: HeXamon switch screen button layout
        buttons = message.buttons or []
        if buttons:
            btn_texts = [btn.text.strip().lower() for row in buttons for btn in row]
            has_view_team = any("team" in b or "party" in b for b in btn_texts)
            has_digits = any(re.search(r"\b[1-6]\b", b) for b in btn_texts)
            has_back = any("back" in b or "cancel" in b for b in btn_texts)

            if (has_view_team or (has_digits and has_back)) and (
                "fainted" in clean or "switch" in clean or has_party_list
            ):
                return True

            if has_digits and (has_view_team or has_back):
                return True

        return False

    def is_battle_end(self, message: Message) -> bool:
        """Checks if a battle concluded (enemy fainted or captured)."""
        if not message:
            return False

        # CRITICAL: A switch prompt (e.g. player's Pokémon fainted) is an active battle turn, NOT an end!
        if self.is_switch_prompt(message):
            return False

        # If message has switch buttons (slot digits 2-6 or view team), it's a switch menu, never battle end
        buttons = message.buttons or []
        if buttons:
            btn_texts = [btn.text.strip().lower() for row in buttons for btn in row]
            if any("team" in b or "party" in b for b in btn_texts) or any(re.search(r"\b[2-6]\b", b) for b in btn_texts):
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

        # Switch prompt belongs directly to BattleHandler!
        if self.is_switch_prompt(message):
            return True

        text = (message.raw_text or "").lower()

        # Initial wild spawns (e.g. "appeared") belong to SpawnHandler, not BattleHandler
        if any(w in text for w in ("appeared", "what will you do", "choose your ball")) and not any(
            b in text for b in ("battle begins", "battle started", "current turn:", "turn:", "hp")
        ):
            return False

        buttons = message.buttons or []

        # 1. Button-based detection (most reliable indicator)
        if buttons:
            button_texts = [btn.text.strip().lower() for row in buttons for btn in row]

            # Battle utility buttons (Run, Pokemons, Switch, Bag) - excludes EV Yield
            if any(any(u in b for u in BATTLE_UTILITY_BUTTONS) for b in button_texts):
                return True

            # Action / sub-menu buttons (Fight, Attack, Moves, Continue, Next)
            if any(any(re.search(rf"\b{re.escape(a)}\b", b) for a in ACTION_BUTTON_NAMES) for b in button_texts):
                return True

            # Check if any button matches a known Pokémon move
            for b_text in button_texts:
                clean_name = self.pokedex.clean_name(b_text)
                if self.pokedex.get_move(clean_name):
                    return True

            # Check if buttons contain both Pokéballs and other choices in a wild/battle context
            has_ball = any(is_pokeball_button(b, self.pokedex) for b in button_texts)
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

    def parse_battle_state(self, message_or_text: any) -> dict:
        """Extracts wild Pokémon name, star symbol, HP, and player info from battle text/message."""
        if isinstance(message_or_text, str):
            text = message_or_text
            msg = None
        else:
            msg = message_or_text
            text = (getattr(msg, "raw_text", "") or "") if msg else ""

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

        # Check for star symbol '☆', unicode variants, or Telegram Custom Emoji
        if check_has_star(msg, text):
            state["has_star"] = True

        # Extract wild Pokémon: "Wild Metapod [Bug]", "Wild Metapod ☆ [Bug]", or "Wild Tangrowth (Lv. 48) has appeared! ☆"
        wild_match = re.search(r"wild\s+([A-Za-z0-9\-]+)(?:\s*\([^\)]+\))?(?:\s*\[(.*?)\])?", text, re.IGNORECASE)
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

                # 1. Check if it's a utility, pokeball menu, or info button (Run, Bag, Pokeballs, Switch, EV Yield, etc.)
                if (is_pokeball_menu_button(label) or
                    any(u in label_lower for u in UTILITY_BUTTON_NAMES) or
                    any(k in label_lower for k in ("ev", "yield", "pokedex", "dex", "info", "guide", "stat"))):
                    utilities[label] = btn
                    continue

                # 2. Check if it's a genuine Pokéball button (MUST NEVER match moves like Electro Ball or Gyro Ball)
                if is_pokeball_button(label, self.pokedex):
                    balls[label] = btn
                    continue

                # 3. Check if it's an action/continue/fight or battle button
                if any(k in label_lower for k in ("battle", "battles", "fight")) or any(re.search(rf"\b{re.escape(a)}\b", label_lower) for a in ACTION_BUTTON_NAMES) or ">>" in label_lower:
                    actions[label] = btn
                    continue

                # 4. Filter out any remaining non-move buttons
                if any(k in label_lower for k in ("run", "bag", "switch", "pokemon", "item", "cancel")):
                    utilities[label] = btn
                    continue

                # 5. Otherwise, it's a move button!
                moves[label] = btn

        return moves, balls, utilities, actions

    def _schedule_battle_watchdog(self, client: TelegramClient, account_name: str, chat_id: any, msg_id: int):
        """Schedules a safety watchdog to re-check the battle message if Telegram edit event was missed."""
        async def watchdog():
            watchdog_delay = 1.5 if getattr(config, "FAST_BATTLE", True) else 2.5
            await asyncio.sleep(watchdog_delay)
            battle = self.active_battles.get(account_name)
            if not battle or battle.get("msg_id") != msg_id:
                return

            now = asyncio.get_event_loop().time()
            if now - battle.get("last_click", 0) < (watchdog_delay - 0.2):
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

    async def handle_auto_switch(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """
        Automatically selects and switches to an available (healthy/alive) Pokémon
        when prompted during battle.
        """
        raw_text = message.raw_text or ""
        buttons = message.buttons or []

        if not buttons:
            logger.warning("[%s] Auto-switch: No buttons present on switch message.", account_name)
            return False

        # 1. Parse party slots from text
        # Format example:
        # 1. Metagross (Fainted)
        # 2. Delphox
        # 3. Primarina
        # 4. Gengar
        # 5. Iron Valiant
        # 6. Corviknight
        party_members = []
        for line in raw_text.splitlines():
            line_str = line.strip()
            match = re.match(r"^([1-6])[\.\:\)]\s*([^\(\[\n\r]+?)(?:\s*[\(\[](.*?)[\)\]])?\s*$", line_str)
            if match:
                slot = int(match.group(1))
                raw_poke_name = match.group(2).strip()
                clean_poke_name = self.pokedex.clean_name(raw_poke_name)
                status = (match.group(3) or "").strip().lower()
                is_fainted = any(f in status for f in ("faint", "fnt", "ko", "0 hp", "dead")) or bool(re.search(r"\b0\s*/", status))
                is_active = any(a in status for a in ("active", "in battle", "currently"))
                party_members.append({
                    "slot": slot,
                    "name": clean_poke_name or raw_poke_name,
                    "status": status,
                    "fainted": is_fainted,
                    "active": is_active
                })

        # 2. Extract wild Pokémon info to optimize switch choice
        wild_name = "Unknown"
        wild_match = re.search(r"wild\s+([A-Za-z0-9\-]+)", raw_text, re.IGNORECASE)
        if wild_match:
            wild_name = wild_match.group(1).title()
        elif account_name in self.active_battles:
            wild_name = self.active_battles[account_name].get("wild_name", "Unknown")

        wild_types = []
        if wild_name and wild_name != "Unknown":
            wild_data = self.pokedex.get_pokemon(wild_name)
            if wild_data:
                wild_types = wild_data.get("types", [])

        # 3. Map buttons to slots and Pokémon
        # Buttons in the switch screen can be numbers (e.g. "2", "3") or Pokémon names.
        # Fainted slots may have empty string/space buttons like " ".
        slot_buttons = {}
        name_buttons = {}
        all_candidate_buttons = []

        for row in buttons:
            for btn in row:
                btn_text = btn.text.strip()
                if not btn_text:
                    continue  # Blank/disabled button for fainted Pokémon
                btn_lower = btn_text.lower()
                if any(k in btn_lower for k in ("view team", "view party", "back", "cancel", "run")):
                    continue

                if btn_text.isdigit() and 1 <= int(btn_text) <= 6:
                    slot_buttons[int(btn_text)] = btn
                    all_candidate_buttons.append((int(btn_text), btn_text, btn))
                else:
                    clean_b = self.pokedex.clean_name(btn_text).lower()
                    name_buttons[clean_b] = btn
                    all_candidate_buttons.append((None, btn_text, btn))

        # 4. Identify alive candidate Pokémon
        eligible = []
        if party_members:
            for member in party_members:
                if member["fainted"] or member["active"]:
                    continue

                slot = member["slot"]
                poke_name = member["name"]
                clean_name = self.pokedex.clean_name(poke_name).lower()

                # Find associated button
                target_btn = slot_buttons.get(slot)
                if not target_btn:
                    target_btn = name_buttons.get(clean_name)
                if not target_btn:
                    for k, b in name_buttons.items():
                        if clean_name in k or k in clean_name:
                            target_btn = b
                            break

                if target_btn:
                    # Calculate type effectiveness score against wild Pokémon
                    score = 1.0
                    if wild_types:
                        poke_data = self.pokedex.get_pokemon(poke_name)
                        if poke_data:
                            types = poke_data.get("types", [])
                            if types:
                                score = max(
                                    [self.pokedex.get_type_multiplier(t, wild_types) for t in types],
                                    default=1.0
                                )
                    eligible.append({
                        "slot": slot,
                        "name": poke_name,
                        "button": target_btn,
                        "score": score
                    })

        # 5. Fallback if party members couldn't be parsed from text
        if not eligible and all_candidate_buttons:
            for slot_num, text_val, btn in all_candidate_buttons:
                eligible.append({
                    "slot": slot_num or 99,
                    "name": text_val,
                    "button": btn,
                    "score": 1.0
                })

        if not eligible:
            logger.warning("[%s] 💀 Auto-switch: No alive Pokémon or clickable switch buttons found!", account_name)
            # If all are fainted, user has lost / whited out
            # Dismiss with Back if available so battle doesn't hang
            for row in buttons:
                for btn in row:
                    if btn.text.strip().lower() in ("back", "cancel"):
                        try:
                            await btn.click()
                        except Exception:
                            pass
                        break
            return False

        # 6. Rank candidates: highest type effectiveness first; if tied, lowest slot (first in party)
        eligible.sort(key=lambda x: (-x["score"], x["slot"]))
        chosen = eligible[0]
        chosen_btn = chosen["button"]

        if getattr(config, "FAST_BATTLE", True):
            delay = random.uniform(
                getattr(config, "FAST_BATTLE_MIN_DELAY", 0.05),
                getattr(config, "FAST_BATTLE_MAX_DELAY", 0.20)
            )
        else:
            delay = random.uniform(1.0, 2.0)

        logger.info(
            "[%s] 🔄 [AUTO SWITCH] Switching to %s (Slot %s, Matchup Score: %.1fx) vs Wild %s in %.2fs...",
            account_name, chosen["name"], chosen["slot"], chosen["score"], wild_name, delay
        )

        if delay > 0:
            await asyncio.sleep(delay)

        self.active_battles[account_name] = {
            "msg_id": message.id,
            "chat_id": message.chat_id,
            "wild_name": wild_name,
            "last_click": asyncio.get_event_loop().time()
        }

        try:
            await asyncio.wait_for(chosen_btn.click(), timeout=3.0)
            click_success = True
        except asyncio.TimeoutError:
            logger.debug("[%s] Auto-switch button click timed out (dispatched).", account_name)
            click_success = True
        except Exception as e:
            logger.error("[%s] Failed to click auto-switch button: %s", account_name, e)
            click_success = False

        self._schedule_battle_watchdog(client, account_name, message.chat_id, message.id)
        return click_success

    async def handle_battle_turn(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """Executes a battle decision based on Hunt Category ('kill' vs 'catch')."""
        if self.is_switch_prompt(message):
            if not self.get_auto_switch(account_name):
                logger.info("[%s] ⏸️ Pokémon switch prompted, but auto-switch is disabled.", account_name)
                return False
            return await self.handle_auto_switch(client, account_name, message)

        raw_text = message.raw_text or ""
        state = self.parse_battle_state(message)

        # Inherit star status from spawn encounter if known, or persist if detected in battle
        if self.starred_encounters.get(account_name, False) or state["has_star"]:
            state["has_star"] = True
            self.starred_encounters[account_name] = True

        moves, balls, utilities, actions = self.categorize_buttons(message)

        if not moves and not balls and not actions and not utilities:
            logger.warning("[%s] No moves, ball buttons, bag/utility, or action buttons available in battle turn.", account_name)
            return False

        star_tag = " [☆ Caught]" if state["has_star"] else " [New]"
        logger.info("[%s] ⚔️ Battle Turn vs Wild %s%s (HP: %d/%d) | Moves: %s | Actions: %s | Balls: %s",
                    account_name, state["wild_name"], star_tag,
                    state["wild_hp"], state["wild_max_hp"],
                    list(moves.keys()), list(actions.keys()), list(balls.keys()))

        chosen_button = None
        action_desc = ""

        # Determine Hunt Category: 'kill' vs 'catch'
        hunt_mode = self.get_hunt_mode(account_name)

        # =========================================================================
        # CATEGORY 1: KILL MODE (FARMING PD - POKÉDOLLARS & EXP)
        # Note: If Pokémon has '☆' star, ALWAYS use Repeat Ball catch regardless of kill mode!
        # =========================================================================
        if hunt_mode == "kill" and not state["has_star"]:
            if moves:
                ranked = self.pokedex.rank_moves_for_damage(
                    list(moves.keys()), state["my_types"], state["wild_types"]
                )
                best_move_name, score = ranked[0]
                chosen_button = moves[best_move_name]
                action_desc = f"[HUNT: KILL] Attacking with {best_move_name} (Damage score: {score:.1f})"
            elif actions:
                # If there are battle/continue/next or fight buttons
                battle_btn = next((btn for label, btn in actions.items() if any(c in label.lower() for c in ("battle", "fight"))), None)
                continue_btn = next((btn for label, btn in actions.items() if any(c in label.lower() for c in ("continue", "next", "proceed", ">>", "ok"))), None)
                chosen_button = battle_btn or continue_btn or list(actions.values())[0]
                action_desc = f"[HUNT: KILL] Advancing battle turn with '{chosen_button.text}'"
            elif balls:
                # Fallback if no moves are left (e.g. Struggle or out of PP)
                chosen_button = list(balls.values())[0]
                action_desc = "[HUNT: KILL] No moves available, throwing ball fallback"

        # =========================================================================
        # CATEGORY 2: CATCH MODE (CAPTURE POKÉMON WITH BALLS)
        # =========================================================================
        else:
            # Check for Pokéball menu opener button (e.g. 'Pokeballs', 'Pokéballs', 'Balls', 'Bag')
            pokeballs_menu_btn = None
            # 1. Highest priority: dedicated Pokéball menu buttons (Pokeballs, Pokéballs, Balls, Throw Ball)
            for label, btn in utilities.items():
                if is_pokeball_menu_button(label) and not any(k in label.lower() for k in ("bag", "backpack")):
                    pokeballs_menu_btn = btn
                    break
            if not pokeballs_menu_btn:
                for label, btn in actions.items():
                    if is_pokeball_menu_button(label) and not any(k in label.lower() for k in ("bag", "backpack")):
                        pokeballs_menu_btn = btn
                        break

            # 2. Second priority: Bag / Backpack buttons
            if not pokeballs_menu_btn:
                for label, btn in utilities.items():
                    if any(k in label.lower() for k in ("bag", "backpack")) or "🎒" in label:
                        pokeballs_menu_btn = btn
                        break
            if not pokeballs_menu_btn:
                for label, btn in actions.items():
                    if any(k in label.lower() for k in ("bag", "backpack")) or "🎒" in label:
                        pokeballs_menu_btn = btn
                        break

            # 3. Last fallback: Item button ONLY if no moves are available and no other menu button exists
            if not pokeballs_menu_btn and not moves:
                for label, btn in utilities.items():
                    if "item" in label.lower():
                        pokeballs_menu_btn = btn
                        break

            # If balls are on screen, reset bag_opened tracker so future turns can re-open Bag if needed
            if balls:
                self.bag_opened.pop(account_name, None)

            # Rule 1: '☆' detected -> Pokémon was already caught once -> USE REPEAT BALL!
            if state["has_star"]:
                repeat_btn = None
                # Check in balls first
                for label, btn in balls.items():
                    if "repeat" in label.lower():
                        repeat_btn = btn
                        break

                # If not in balls, check all buttons on the message directly
                if not repeat_btn and message.buttons:
                    for row in message.buttons:
                        for btn in row:
                            if "repeat" in btn.text.lower():
                                repeat_btn = btn
                                break
                        if repeat_btn:
                            break

                if repeat_btn:
                    chosen_button = repeat_btn
                    action_desc = f"[HUNT: CATCH] 🔁 Pokémon has '☆' -> Throwing Repeat Ball at {state['wild_name']}"
                elif pokeballs_menu_btn and (self.bag_opened.get(account_name) != message.id or not moves):
                    # Repeat Ball is not on screen, but Pokeballs menu / Bag is available -> OPEN POKEBALLS MENU!
                    self.bag_opened[account_name] = message.id
                    chosen_button = pokeballs_menu_btn
                    action_desc = f"[HUNT: CATCH] 🔁 [☆ Star Detected] Opening Pokéballs menu via '{pokeballs_menu_btn.text}' to access Repeat Ball for {state['wild_name']}"
                elif balls:
                    # Repeat Ball is truly missing (e.g. out of stock in Bag) -> throw fallback ball
                    fallback_ball_btn = None
                    for pref in config.PREFERRED_BALLS:
                        pref_clean = pref.lower().replace(" ", "").replace("ball", "")
                        for label, btn in balls.items():
                            clean_l = label.lower().replace(" ", "").replace("ball", "")
                            if pref_clean in clean_l:
                                fallback_ball_btn = btn
                                break
                        if fallback_ball_btn:
                            break
                    chosen_button = fallback_ball_btn or list(balls.values())[0]
                    action_desc = f"[HUNT: CATCH] 🔁 Has '☆' but Repeat Ball missing -> Throwing {chosen_button.text}"
                elif actions:
                    chosen_button = list(actions.values())[0]
                    action_desc = f"[HUNT: CATCH] Advancing turn with '{chosen_button.text}'"
                elif moves:
                    ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                    chosen_button = moves[ranked[0][0]]
                    action_desc = f"[HUNT: CATCH] No balls left -> Attacking {state['wild_name']} with {ranked[0][0]}"

            # Rule 2: Not caught yet (No '☆')
            else:
                rarity = self.pokedex.get_rarity_tier(state["wild_name"])

                if rarity in ("legendary", "mythical", "rare", "starter"):
                    # High priority catch: Ultra/Master Ball
                    target_ball_btn = None
                    for pref in ["master", "ultra", "great", "poke", "regular"]:
                        for label, btn in balls.items():
                            if pref in label.lower():
                                target_ball_btn = btn
                                break
                        if target_ball_btn:
                            break

                    if target_ball_btn:
                        chosen_button = target_ball_btn
                        action_desc = f"[HUNT: CATCH] 🌟 {rarity.upper()} encounter -> Throwing {target_ball_btn.text} at {state['wild_name']}"
                    elif balls:
                        chosen_button = list(balls.values())[0]
                        action_desc = f"[HUNT: CATCH] 🌟 Throwing {chosen_button.text} at {rarity} {state['wild_name']}"
                    elif pokeballs_menu_btn and (self.bag_opened.get(account_name) != message.id or not moves):
                        self.bag_opened[account_name] = message.id
                        chosen_button = pokeballs_menu_btn
                        action_desc = f"[HUNT: CATCH] 🌟 Opening Pokéballs menu via '{pokeballs_menu_btn.text}' to access Ultra/Master Ball for {rarity} {state['wild_name']}"
                    elif moves:
                        # Weaken carefully with safe move
                        safe_move, score = self.pokedex.find_safe_move_to_weaken(
                            list(moves.keys()), state["my_types"], state["wild_types"]
                        )
                        chosen_button = moves[safe_move]
                        action_desc = f"[HUNT: CATCH] Weakening {rarity} {state['wild_name']} with safe move {safe_move}"
                    elif actions:
                        chosen_button = list(actions.values())[0]
                        action_desc = f"[HUNT: CATCH] Advancing turn with '{chosen_button.text}'"
                else:
                    # Common Pokémon
                    if hunt_mode == "hybrid" and getattr(config, "HYBRID_KILL_COMMONS", False) and moves:
                        ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                        best_move, score = ranked[0]
                        chosen_button = moves[best_move]
                        action_desc = f"[HUNT: CATCH] Defeating common {state['wild_name']} with {best_move} for PD"
                    elif balls:
                        regular_btn = None
                        for pref in ["regular", "poke", "great", "ultra", "master"]:
                            for label, btn in balls.items():
                                if pref in label.lower():
                                    regular_btn = btn
                                    break
                            if regular_btn:
                                break

                        chosen_button = regular_btn or list(balls.values())[0]
                        action_desc = f"[HUNT: CATCH] Throwing {chosen_button.text} at common {state['wild_name']}"
                    elif pokeballs_menu_btn and (self.bag_opened.get(account_name) != message.id or not moves):
                        self.bag_opened[account_name] = message.id
                        chosen_button = pokeballs_menu_btn
                        action_desc = f"[HUNT: CATCH] 🎒 Opening Pokéballs menu via '{pokeballs_menu_btn.text}' to access Pokéballs for common {state['wild_name']}"
                    elif actions:
                        chosen_button = list(actions.values())[0]
                        action_desc = f"[HUNT: CATCH] Advancing turn with '{chosen_button.text}'"
                    elif moves:
                        ranked = self.pokedex.rank_moves_for_damage(list(moves.keys()), state["my_types"], state["wild_types"])
                        chosen_button = moves[ranked[0][0]]
                        action_desc = f"[HUNT: CATCH] Out of balls -> Attacking {state['wild_name']} with {ranked[0][0]}"


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

    def handle_battle_result(self, account_name: str, message: Message, client: Optional[TelegramClient] = None):
        """Processes end of battle rewards and statistics."""
        # Dismiss any end screen buttons (Continue, Next, OK, Claim, Back) so bot clears battle state
        if message and message.buttons:
            for row in message.buttons:
                for btn in row:
                    clean = btn.text.strip().lower()
                    if any(c in clean for c in ("continue", "next", "ok", "claim", "back", ">>", "proceed")):
                        try:
                            logger.info("[%s] 👆 Dismissing battle end screen button '%s'...", account_name, btn.text)
                            asyncio.create_task(btn.click())
                        except Exception as e:
                            logger.debug("[%s] Error dismissing end button: %s", account_name, e)
                        break

        # Clear from active battles, bag tracking, and star tracking
        self.active_battles.pop(account_name, None)
        self.starred_encounters.pop(account_name, None)
        self.bag_opened.pop(account_name, None)
        if self.spawn_handler and hasattr(self.spawn_handler, "active_encounters"):
            self.spawn_handler.active_encounters.pop(account_name, None)

        text = (message.raw_text if message else "") or ""
        text_lower = text.lower()

        # Check for PD (Pokédollars) earned
        pd_match = re.search(r"(\d+(?:,\d+)?)\s*(?:pd|pokédollars|pokedollars)", text_lower)
        is_catch = False

        if any(w in text_lower for w in ("caught", "congratulations", "gotcha", "was caught", "captured")):
            is_catch = True
            if "repeat" in text_lower or "☆" in text:
                self.stats["caught_repeat"] += 1
                logger.info("[%s] 🔁 Caught Pokémon with Repeat Ball! Total Repeat: %d",
                            account_name, self.stats["caught_repeat"])
            else:
                self.stats["caught_regular"] += 1
                logger.info("[%s] 🎉 Caught new Pokémon! Total: %d",
                            account_name, self.stats["caught_regular"])
            if pd_match:
                pd_amount = int(pd_match.group(1).replace(",", ""))
                self.stats["pd_earned"] += pd_amount

        elif pd_match:
            pd_amount = int(pd_match.group(1).replace(",", ""))
            self.stats["pd_earned"] += pd_amount
            self.stats["battles_won"] += 1
            logger.info("[%s] 💰 BATTLE VICTORY! Earned %d PD! (Total Won: %d, Total PD: %d)",
                        account_name, pd_amount, self.stats["battles_won"], self.stats["pd_earned"])

        elif any(w in text_lower for w in ("blacked out", "whited out", "lost the battle")):
            logger.info("[%s] 💀 All Pokémon fainted. Blacked out / lost the battle.", account_name)

        elif "fainted" in text_lower or "defeated" in text_lower or "won" in text_lower or "victory" in text_lower:
            if "wild" in text_lower or any(w in text_lower for w in ("won", "victory", "defeated")):
                self.stats["battles_won"] += 1
                logger.info("[%s] 💥 Wild Pokémon fainted/defeated! Battle won (Total: %d).",
                            account_name, self.stats["battles_won"])
            else:
                logger.info("[%s] 💥 Battle concluded.", account_name)

        elif any(w in text_lower for w in ("fled", "ran away", "escaped", "flew away", "got away")):
            logger.info("[%s] 💨 Wild Pokémon fled from battle. Encounter concluded.", account_name)

        # Signal that the battle has fully completed so AutoHunter can proceed to next hunt
        if self.on_battle_end:
            try:
                import inspect
                sig = inspect.signature(self.on_battle_end)
                if "is_catch" in sig.parameters:
                    self.on_battle_end(account_name, is_catch=is_catch)
                else:
                    self.on_battle_end(account_name)
            except Exception as e:
                logger.error("[%s] Error in on_battle_end callback: %s", account_name, e)
