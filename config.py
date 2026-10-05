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

# Auto-Guess settings (/guess "Who's that Pokémon?" PD farming)
AUTO_GUESS_ENABLED = os.getenv("AUTO_GUESS_ENABLED", "True").lower() in ("true", "1", "yes")
GUESS_COMMAND = os.getenv("GUESS_COMMAND", "/guess")
GUESS_INTERVAL = float(os.getenv("GUESS_INTERVAL", "5.0"))
GUESS_JITTER = float(os.getenv("GUESS_JITTER", "2.0"))

# Auto-Catch settings
AUTO_CATCH_ENABLED = os.getenv("AUTO_CATCH_ENABLED", "True").lower() in ("true", "1", "yes")
PREFERRED_BALLS = [b.strip() for b in os.getenv("PREFERRED_BALLS", "Masterball,Ultraball,Greatball,Repeatball,Regularball,Pokeball").split(",")]

# Battle System Strategy:
# - "kill": Only killing system to defeat wild Pokémon and farm PD (Pokédollars)
# - "hybrid": Hybrid by rarity (Repeat Ball for '☆', Ultra/Master for Legendaries/Rares)
BATTLE_SYSTEM = os.getenv("BATTLE_SYSTEM", "hybrid").lower().strip()
HYBRID_KILL_COMMONS = os.getenv("HYBRID_KILL_COMMONS", "False").lower() in ("true", "1", "yes")

# Path to pokedex.json
POKEDEX_FILE = BASE_DIR / "pokedex.json"
ALERT_USER_ID = int(os.getenv("ALERT_USER_ID", "0"))
ALERT_BOT_TOKEN = os.getenv("ALERT_BOT_TOKEN", "")

# Human reaction delays (seconds)
MIN_REACTION_DELAY = float(os.getenv("MIN_REACTION_DELAY", "2.5"))
MAX_REACTION_DELAY = float(os.getenv("MAX_REACTION_DELAY", "5.0"))

# Sessions directory
SESSIONS_DIR = BASE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# Data directory
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# Path to local knowledge base
KNOWN_CHECKS_FILE = DATA_DIR / "known_checks.json"

# Path to pre-computed local sprite masks cache
SPRITE_CACHE_FILE = DATA_DIR / "sprite_cache.pkl"
