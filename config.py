import os
from pathlib import Path
from dotenv import load_dotenv

# Base directory
BASE_DIR = Path(__file__).resolve().parent

# Load .env file
load_dotenv(BASE_DIR / ".env")

# Telegram API settings
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# Gemini API settings
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

# Target Pokémon bot & Chat
TARGET_BOT = os.getenv("TARGET_BOT", "HeXamonbot").lstrip("@")
HUNT_CHAT = os.getenv("HUNT_CHAT", TARGET_BOT)

# Operating Mode:
# - "guess": Only run the /guess "Who's that Pokémon?" PD farming loop
# - "hunt":  Only run the /hunt encounter and battle loop
# - "both":  Run both /hunt and /guess concurrently
ACTION_MODE = os.getenv("ACTION_MODE", "guess").lower().strip()

# Auto-Hunt settings
AUTO_HUNT_ENABLED = os.getenv("AUTO_HUNT_ENABLED", "True").lower() in ("true", "1", "yes")
HUNT_COMMAND = os.getenv("HUNT_COMMAND", "/hunt")
HUNT_INTERVAL = float(os.getenv("HUNT_INTERVAL", "12.0"))
HUNT_JITTER = float(os.getenv("HUNT_JITTER", "3.0"))
HUNT_INSTA_FLASH = os.getenv("HUNT_INSTA_FLASH", "True").lower() in ("true", "1", "yes")
HUNT_MIN_DELAY = float(os.getenv("HUNT_MIN_DELAY", "1.0"))
HUNT_MAX_DELAY = float(os.getenv("HUNT_MAX_DELAY", "1.5"))

# Telegram Custom Emoji IDs for previously caught / owned Pokémon star (Repeat Ball)
STAR_CUSTOM_EMOJI_IDS = [
    int(x.strip()) for x in os.getenv("STAR_CUSTOM_EMOJI_IDS", "5258185631355378853").split(",")
    if x.strip().isdigit()
]

# Fast Battle & Catch settings (lightning-fast turn attacks and ball throws)
FAST_BATTLE = os.getenv("FAST_BATTLE", "True").lower() in ("true", "1", "yes")
FAST_BATTLE_MIN_DELAY = float(os.getenv("FAST_BATTLE_MIN_DELAY", "0.05"))
FAST_BATTLE_MAX_DELAY = float(os.getenv("FAST_BATTLE_MAX_DELAY", "0.20"))

# Auto-Switch settings (automatically switches to an alive Pokémon when active Pokémon faints)
AUTO_SWITCH_ENABLED = os.getenv("AUTO_SWITCH_ENABLED", "True").lower() in ("true", "1", "yes")

# Auto-Guess settings (/guess "Who's that Pokémon?" PD farming)
AUTO_GUESS_ENABLED = os.getenv("AUTO_GUESS_ENABLED", "True").lower() in ("true", "1", "yes")
GUESS_COMMAND = os.getenv("GUESS_COMMAND", "/guess")
GUESS_INTERVAL = float(os.getenv("GUESS_INTERVAL", "5.0"))
GUESS_JITTER = float(os.getenv("GUESS_JITTER", "2.0"))
GUESS_INSTA_FLASH = os.getenv("GUESS_INSTA_FLASH", "True").lower() in ("true", "1", "yes")
GUESS_MIN_DELAY = float(os.getenv("GUESS_MIN_DELAY", "0.05"))
GUESS_MAX_DELAY = float(os.getenv("GUESS_MAX_DELAY", "0.20"))

# Auto-Catch settings
AUTO_CATCH_ENABLED = os.getenv("AUTO_CATCH_ENABLED", "True").lower() in ("true", "1", "yes")
PREFERRED_BALLS = [b.strip() for b in os.getenv("PREFERRED_BALLS", "Masterball,Ultraball,Greatball,Repeatball,Regularball,Pokeball").split(",")]

# Hunt Category / Strategy:
# - "kill":  Attack & defeat wild Pokémon with highest damage moves to farm PD & EXP
# - "catch": Capture wild Pokémon with Pokéballs (Repeat Ball for '☆', Ultra/Master for Rares, Regular/Poke for Commons)
HUNT_MODE = os.getenv("HUNT_MODE", os.getenv("BATTLE_SYSTEM", "kill")).lower().strip()
if HUNT_MODE in ("hybrid", "catch"):
    HUNT_MODE = "catch"
elif HUNT_MODE not in ("kill", "catch"):
    HUNT_MODE = "kill"
BATTLE_SYSTEM = HUNT_MODE
HYBRID_KILL_COMMONS = os.getenv("HYBRID_KILL_COMMONS", "False").lower() in ("true", "1", "yes")

# Path to pokedex.json
POKEDEX_FILE = BASE_DIR / "pokedex.json"
ALERT_USER_ID = int(os.getenv("ALERT_USER_ID", "0"))
ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", ALERT_BOT_TOKEN)
ADMIN_USER_IDS = [int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", str(ALERT_USER_ID)).split(",") if x.strip() and x.strip() != "0"]

# Human reaction delays (seconds)
MIN_REACTION_DELAY = float(os.getenv("MIN_REACTION_DELAY", "2.5"))
MAX_REACTION_DELAY = float(os.getenv("MAX_REACTION_DELAY", "5.0"))

# Storage directory for persistent database & sessions (supports Railway volume)
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", os.getenv("DATA_DIR", str(BASE_DIR / "data"))))
STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# Static data directory shipped with the repository
DATA_DIR = BASE_DIR / "data"

# Path to database file
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", str(STORAGE_DIR / "autohexa.db")))

# Sessions directory
SESSIONS_DIR = Path(os.getenv("SESSIONS_DIR", str(STORAGE_DIR / "sessions")))
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

# Path to local knowledge base
KNOWN_CHECKS_FILE = DATA_DIR / "known_checks.json"

# Path to pre-computed local sprite masks cache
SPRITE_CACHE_FILE = DATA_DIR / "sprite_cache.pkl"
