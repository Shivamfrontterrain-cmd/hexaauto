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

# Moves that end in "ball" or contain "ball" in Pokémon games - these are attack moves, NEVER Pokéballs!
KNOWN_BALL_MOVES = {
    "electro ball", "electroball",
    "gyro ball", "gyroball",
    "energy ball", "energyball",
    "shadow ball", "shadowball",
    "mist ball", "mistball",
    "weather ball", "weatherball",
    "ice ball", "iceball",
    "pyro ball", "pyroball"
}

# Recognized Pokéball type keywords
POKEBALL_TYPE_PREFIXES = (
    "poke", "poké", "great", "ultra", "master", "safari", "fast", "level",
    "lure", "heavy", "love", "friend", "moon", "sport", "park", "net",
    "dive", "nest", "repeat", "timer", "luxury", "premier", "dusk", "heal",
    "quick", "cherish", "dream", "beast", "strange", "origin", "feather",
    "wing", "jet", "leaden", "gigaton", "regular"
)

def is_pokeball_button(label: str, pokedex: Optional["PokedexService"] = None) -> bool:
    """
    Accurately determines whether a Telegram button represents a Pokéball.
    Explicitly filters out attack moves like 'Electro Ball', 'Gyro Ball', and 'Shadow Ball'.
    """
    if not label:
        return False

    clean = re.sub(r"[\(\[\{].*?[\)\]\}]", "", label)
    clean = re.sub(r"\bx\d+\b", "", clean, flags=re.IGNORECASE).strip()
    clean_lower = clean.lower()

    # 1. Moves with "ball" in the name are NEVER Pokéballs
    if any(m in clean_lower for m in KNOWN_BALL_MOVES):
        return False

    # 2. Check against pokedex move database if available
    if pokedex:
        cleaned_for_pokedex = pokedex.clean_name(label)
        if pokedex.get_move(cleaned_for_pokedex):
            return False

    # 3. Exact matches for standard Pokéballs
    if clean_lower in ("regular", "regularball", "regular ball", "pokeball", "poke ball", "poké ball", "pokéball"):
        return True

    # 4. Ball with valid Pokéball type prefix (e.g. Ultra Ball, Repeat Ball, Great Ball, etc.)
    if ("ball" in clean_lower or "balls" in clean_lower) and any(p in clean_lower for p in POKEBALL_TYPE_PREFIXES):
        return True

    return False

STAR_CUSTOM_EMOJI_IDS = {5258185631355378853, "5258185631355378853"}

def check_has_star(message: Optional[any] = None, text: Optional[str] = None) -> bool:
    """
    Accurately determines whether a wild Pokémon encounter or battle has a star '☆'
    (indicating the Pokémon was previously caught/owned, requiring Repeat Ball catch).
    Detects:
    1. Unicode star symbols ('☆', '★', '⭐', '🌟', '✨', '[☆') in text/raw_text/labels.
    2. Telegram Custom Emoji entities matching the star emoji id:
       5258185631355378853 (or configured in config.STAR_CUSTOM_EMOJI_IDS).
    """
    target_texts = []
    if text:
        target_texts.append(str(text))
    if message:
        raw_text = getattr(message, "raw_text", None)
        if raw_text:
            target_texts.append(str(raw_text))
        msg_text = getattr(message, "text", None)
        if msg_text and msg_text != raw_text:
            target_texts.append(str(msg_text))

    star_symbols = ("☆", "★", "⭐", "🌟", "✨", "[☆")
    for t in target_texts:
        if any(s in t for s in star_symbols):
            return True

    # Check Telegram Custom Emoji entities
    if message and hasattr(message, "entities") and message.entities:
        star_ids = set(STAR_CUSTOM_EMOJI_IDS)
        try:
            import config
            cfg_ids = getattr(config, "STAR_CUSTOM_EMOJI_IDS", [])
            if isinstance(cfg_ids, (list, tuple, set)):
                for cid in cfg_ids:
                    try:
                        star_ids.add(int(cid))
                    except Exception:
                        pass
                    star_ids.add(str(cid))
        except Exception:
            pass

        for ent in message.entities:
            doc_id = getattr(ent, "document_id", None)
            if doc_id is not None:
                if doc_id in star_ids or str(doc_id) in star_ids:
                    return True

    return False

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

    def is_pokeball(self, label: str) -> bool:
        """Determines whether a button label is a Pokéball and not an attack move."""
        return is_pokeball_button(label, self)


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
