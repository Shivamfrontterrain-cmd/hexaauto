import asyncio
import logging
import random
import re
from typing import Dict, Optional, Callable
from telethon import TelegramClient, utils
from telethon.tl.custom.message import Message
from telethon.tl.types import PhotoStrippedSize

import config
from core.gemini_solver import GeminiSolver
from core.sprite_matcher import SpriteMatcher

logger = logging.getLogger("autohexa.guess_handler")

GUESS_CHALLENGE_KEYWORDS = [
    "who's that pokémon",
    "who's that pokemon",
    "whos that pokemon",
    "who is that pokemon"
]

GUESS_RESULT_KEYWORDS = [
    "correct",
    "it's",
    "it is",
    "was caught",
    "time's up",
    "wrong",
    "better luck"
]

class GuessHandler:
    """Handles the /guess 'Who's that Pokémon?' challenge using Local Sprites + Gemini Vision."""

    def __init__(
        self,
        gemini_solver: GeminiSolver,
        sprite_matcher: Optional[SpriteMatcher] = None,
        on_guess_end: Optional[Callable[[str], None]] = None
    ):
        self.gemini = gemini_solver
        self.sprite_matcher = sprite_matcher
        self.on_guess_end = on_guess_end
        self.stats = {
            "total_guesses": 0,
            "correct_guesses": 0,
            "pd_earned": 0
        }

    def _clean_text(self, text: str) -> str:
        """Normalizes text by removing accents, curly apostrophes, and punctuation."""
        t = text.lower().replace("é", "e").replace("’", "'").replace("`", "'")
        t = re.sub(r"[^\w\s]", " ", t)
        return " ".join(t.split())

    def is_guess_challenge(self, message: Message) -> bool:
        """Checks if the message is a 'Who's that Pokémon?' silhouette image."""
        clean = self._clean_text(message.raw_text or "")
        has_keyword = ("who" in clean and "that" in clean) or "whos that" in clean or "guess" in clean
        has_media = bool(message.media or message.photo)
        return has_keyword and has_media

    def is_guess_result(self, message: Message) -> bool:
        """Checks if the message reports the result of a guess."""
        clean = self._clean_text(message.raw_text or "")
        # Avoid matching wild battle conclusions
        if any(w in clean for w in ["battle", "fainted", "wild", "turn", "lower", "moves", "appeared"]):
            return False
        if any(kw in clean for kw in ["correct", "its", "it is", "times up", "wrong", "guessed", "better luck", "that was"]):
            return True
        return False

    async def handle_challenge(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """
        Resolves the silhouette challenge:
        Tier 1: Local High-Speed Sprite Matcher (<0.2s, 99% accuracy)
        Tier 2: Gemini 3.1 Flash Lite Vision Fallback
        """
        logger.info("[%s] 🖼️ 'Who's that Pokémon?' challenge detected! Resolving silhouette...", account_name)

        try:
            pokemon_name: Optional[str] = None
            confidence: float = 0.0
            source: str = ""
            image_bytes: Optional[bytes] = None

            # --- Stage 0: Instant Embedded Stripped Thumbnail (0ms, 0 network requests!) ---
            photo = getattr(message, "photo", None)
            if photo and hasattr(photo, "sizes") and self.sprite_matcher:
                for s in photo.sizes:
                    if isinstance(s, PhotoStrippedSize):
                        try:
                            stripped_jpg = utils.stripped_photo_to_jpg(s.bytes)
                            if stripped_jpg:
                                s_name, s_conf = self.sprite_matcher.match_silhouette(stripped_jpg)
                                if s_name and s_conf >= 0.88:
                                    pokemon_name = s_name
                                    confidence = s_conf
                                    source = f"Tier 1 (Instant In-Memory Mask 0ms, {s_conf*100:.1f}%)"
                                    image_bytes = stripped_jpg
                                    break
                        except Exception:
                            pass

            # --- Stage 1: Ultra-Fast Medium Thumbnail Download (~10KB, ~40-60ms with cryptg) ---
            if not pokemon_name and self.sprite_matcher:
                try:
                    thumb_bytes = await message.download_media(file=bytes, thumb="m")
                    if thumb_bytes:
                        t_name, t_conf = self.sprite_matcher.match_silhouette(thumb_bytes)
                        if t_name and t_conf >= 0.82:
                            pokemon_name = t_name
                            confidence = t_conf
                            source = f"Tier 1 (Fast Thumbnail, {t_conf*100:.1f}%)"
                            image_bytes = thumb_bytes
                except Exception as e:
                    logger.debug("[%s] Thumbnail download skipped: %s", account_name, e)

            # --- Stage 2: Full Image Download Fallback ---
            if not pokemon_name:
                image_bytes = await message.download_media(file=bytes)
                if not image_bytes:
                    logger.error("[%s] Failed to download image bytes for /guess challenge.", account_name)
                    if self.on_guess_end:
                        self.on_guess_end(account_name)
                    return False

                if self.sprite_matcher:
                    local_name, local_conf = self.sprite_matcher.match_silhouette(image_bytes)
                    if local_name and local_conf >= 0.80:
                        pokemon_name = local_name
                        confidence = local_conf
                        source = f"Tier 1 (Full Sprite Matcher, {local_conf*100:.1f}%)"

            # --- Stage 3: Gemini Vision AI Fallback ---
            if not pokemon_name:
                if not image_bytes:
                    image_bytes = await message.download_media(file=bytes)
                if not image_bytes:
                    logger.error("[%s] Could not obtain image for Gemini Vision.", account_name)
                    if self.on_guess_end:
                        self.on_guess_end(account_name)
                    return False

                logger.info("[%s] Local match not conclusive. Querying Tier 2 (Gemini Vision AI)...", account_name)
                pokemon_name, confidence, reason = await self.gemini.solve_pokemon_silhouette(image_bytes)
                source = "Tier 2 (Gemini Vision)"

                if "API key not valid" in reason or "HTTP error 400" in reason or "INVALID GEMINI KEY" in reason:
                    logger.critical(
                        "\n" + "=" * 70 +
                        "\n [!] CRITICAL: YOUR GEMINI_API_KEY IN .env IS INVALID!" +
                        "\n [!] Get a FREE API key from: https://aistudio.google.com/app/apikey" +
                        "\n" + "=" * 70
                    )
                    if self.on_guess_end:
                        self.on_guess_end(account_name)
                    return False

                if not pokemon_name or confidence < 0.70:
                    logger.warning("[%s] Silhouette could not be identified (Name: %s, Conf: %s, Reason: %s)",
                                   account_name, pokemon_name, confidence, reason)
                    if self.on_guess_end:
                        self.on_guess_end(account_name)
                    return False

            self.stats["total_guesses"] += 1

            # Insta-Flash vs Natural Delay
            if getattr(config, "GUESS_INSTA_FLASH", True):
                delay = random.uniform(getattr(config, "GUESS_MIN_DELAY", 0.05), getattr(config, "GUESS_MAX_DELAY", 0.20))
            else:
                delay = random.uniform(0.6, 1.2)

            logger.info("[%s] ⚡ %s -> '%s'! Sending answer in %.2fs...",
                        account_name, source, pokemon_name, delay)
            if delay > 0:
                await asyncio.sleep(delay)

            # Send answer (try reply first, fallback to direct message)
            try:
                await message.reply(pokemon_name)
            except Exception:
                await client.send_message(message.chat_id, pokemon_name)

            logger.info("[%s] 🎯 Sent guess: '%s'!", account_name, pokemon_name)
            return True

        except Exception as e:
            logger.error("[%s] Error handling /guess challenge: %s", account_name, e)
            if self.on_guess_end:
                self.on_guess_end(account_name)
            return False

    def handle_result(self, account_name: str, message: Message):
        """Processes the outcome of a /guess attempt and records PD rewards."""
        text = message.raw_text or ""
        text_lower = text.lower()

        if "correct" in text_lower or "it's" in text_lower or "gained" in text_lower:
            self.stats["correct_guesses"] += 1
            pd_match = re.search(r"(\d+(?:,\d+)?)\s*(?:pd|pokédollars|pokedollars)", text_lower)
            if pd_match:
                pd_amount = int(pd_match.group(1).replace(",", ""))
                self.stats["pd_earned"] += pd_amount
                logger.info("[%s] 💰 GUESS SUCCESS! Earned %d PD! (Total Correct: %d, Total PD: %d)",
                            account_name, pd_amount, self.stats["correct_guesses"], self.stats["pd_earned"])
            else:
                logger.info("[%s] 💰 GUESS SUCCESS! Total Correct: %d", account_name, self.stats["correct_guesses"])
        else:
            logger.info("[%s] Guess result: %s", account_name, text.strip()[:60])

        # Signal completion to auto_guesser
        if self.on_guess_end:
            try:
                self.on_guess_end(account_name)
            except Exception as e:
                logger.error("[%s] Error in on_guess_end callback: %s", account_name, e)
