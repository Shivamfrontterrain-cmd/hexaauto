import json
import logging
from pathlib import Path
import re
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("autohexa.pokedex")

STARTER_NAMES = {
    "bulbasaur", "ivysaur", "venusaur", "charmander", "charmeleon", "charizard",
    "squirtle", "wartortle", "blastoise", "chikorita", "bayleef", "meganium",
    "cyndaquil", "quilava", "typhlosion", "totodile", "croconaw", "feraligatr",
    "treecko", "grovyle", "sceptile", "torchic", "combusken", "blaziken",
    "mudkip", "marshtomp", "swampert", "turtwig", "grotle", "torterra",
    "chimchar", "monferno", "infernape", "piplup", "prinplup", "empoleon",
    "snivy", "servine", "serperior", "tepig", "pignite", "emboar",
    "oshawott", "dewott", "samurott", "chespin", "quilladin", "chesnaught",
    "fennekin", "braixen", "delphox", "froakie", "frogadier", "greninja",
    "rowlet", "dartrix", "decidueye", "litten", "torracat", "incineroar",
    "popplio", "brionne", "primarina", "grookey", "thwackey", "rillaboom",
    "scorbunny", "raboot", "cinderace", "sobble", "drizzile", "inteleon",
    "sprigatito", "floragato", "meowscarada", "fuecoco", "crocalor", "skeledirge",
    "quaxly", "quaxwell", "quaquaval", "pikachu", "eevee"
}

class PokedexService:
    """Provides fast lookups for Pokémon stats, moves, type effectiveness, and rarity."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self.pokemon: Dict[str, dict] = {}
        self.moves: Dict[str, dict] = {}
        self.types_chart: Dict[str, dict] = {}
        self._load()

    def clean_name(self, name: str) -> str:
        """Strips PP indicators, brackets, parentheses, and special symbols from move/pokemon name."""
        clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", name).strip()
        clean = re.sub(r"[^\w\s\-]", "", clean).strip()
        return clean or name

    def _normalize(self, name: str) -> str:
        clean = self.clean_name(name)
        return clean.lower().strip().replace(" ", "-").replace("'", "").replace(".", "")


    def _load(self):
        if not self.filepath.exists():
            logger.error("pokedex.json not found at %s!", self.filepath)
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.pokemon = data.get("pokemon", {})
                self.moves = data.get("moves", {})
                self.types_chart = data.get("types", {})
            logger.info("PokedexService loaded: %d Pokémon, %d Moves, %d Types.",
                        len(self.pokemon), len(self.moves), len(self.types_chart))
        except Exception as e:
            logger.error("Failed to load pokedex.json: %s", e)

    def get_pokemon(self, name: str) -> Optional[dict]:
        """Looks up a Pokémon by name."""
        key = self._normalize(name)
        if key in self.pokemon:
            return self.pokemon[key]
        # Partial match if needed
        for k, v in self.pokemon.items():
            if k == key or key in k:
                return v
        return None

    def get_move(self, name: str) -> Optional[dict]:
        """Looks up a move by name."""
        key = self._normalize(name)
        if key in self.moves:
            return self.moves[key]
        for k, v in self.moves.items():
            if k == key or key in k:
                return v
        return None

    def get_type_multiplier(self, attack_type: str, defender_types: List[str]) -> float:
        """
        Calculates type effectiveness multiplier of an attacking move against defending types.
        (e.g., Fire vs Bug = 2.0x, Fire vs Bug/Steel = 4.0x, Normal vs Ghost = 0.0x)
        """
        atk_type = attack_type.lower().strip()
        multiplier = 1.0

        for def_type in defender_types:
            def_type_clean = def_type.lower().strip()
            defending_matchups = self.types_chart.get(def_type_clean, {})
            # Look up how def_type reacts to incoming atk_type
            if atk_type in defending_matchups:
                multiplier *= defending_matchups[atk_type]

        return multiplier

    def get_rarity_tier(self, pokemon_name: str) -> str:
        """
        Categorizes Pokémon into rarity tiers:
        - 'legendary'
        - 'mythical'
        - 'starter'
        - 'rare' (catch rate <= 45 or pseudo-legendary)
        - 'common'
        """
        clean = self._normalize(pokemon_name)
        poke_data = self.get_pokemon(clean)

        if poke_data:
            if poke_data.get("is_legendary"):
                return "legendary"
            if poke_data.get("is_mythical"):
                return "mythical"
            catch_rate = poke_data.get("catch_rate", 255)
            if catch_rate <= 45:
                return "rare"

        if clean in STARTER_NAMES:
            return "starter"

        return "common"

    def rank_moves_for_damage(
        self,
        available_moves: List[str],
        attacker_types: Optional[List[str]],
        defender_types: List[str]
    ) -> List[Tuple[str, float]]:
        """
        Ranks moves by expected damage output against defender_types.
        Returns sorted list of tuples (move_name, score) descending.
        """
        ranked = []
        att_types = [t.lower() for t in (attacker_types or [])]

        for move_name in available_moves:
            move_data = self.get_move(move_name)
            if not move_data:
                # Default fallback score for unknown move
                ranked.append((move_name, 40.0))
                continue

            power = move_data.get("p") or 0
            if power == 0:
                # Status move
                ranked.append((move_name, 0.0))
                continue

            move_type = move_data.get("t", "normal")
            mult = self.get_type_multiplier(move_type, defender_types)

            # STAB (Same-Type Attack Bonus): 1.5x damage if attacker shares move type
            stab = 1.5 if move_type in att_types else 1.0
            accuracy = (move_data.get("a") or 100) / 100.0

            score = power * mult * stab * accuracy
            ranked.append((move_name, score))

        # Sort highest score first
        ranked.sort(key=lambda x: x[1], reverse=True)
        return ranked

    def find_safe_move_to_weaken(
        self,
        available_moves: List[str],
        attacker_types: Optional[List[str]],
        defender_types: List[str]
    ) -> Tuple[str, float]:
        """
        Finds a non-lethal, low/moderate power move to safely weaken a Pokémon without fainting it.
        """
        ranked = self.rank_moves_for_damage(available_moves, attacker_types, defender_types)
        # Filter out 0-damage status moves if possible
        damaging_moves = [r for r in ranked if r[1] > 0]
        if not damaging_moves:
            return ranked[0] if ranked else (available_moves[0], 0.0)

        # Pick the lowest damaging move that still deals damage
        return damaging_moves[-1]
