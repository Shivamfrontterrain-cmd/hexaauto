import json
import logging
import re
from typing import List, Optional, Tuple
import aiohttp

logger = logging.getLogger("autohexa.gemini_solver")

class GeminiSolver:
    """Tier 2: High-precision zero-temperature Gemini API solver."""

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        # Support both new 'AQ.' format and legacy 'AIza' format.
        # Auto-trim any accidental leading character (like 'yAQ.')
        cleaned = api_key.strip()
        if cleaned.startswith("yAQ."):
            cleaned = cleaned[1:]
        self.api_key = cleaned
        self.model = model
        self.endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"

    def _is_valid_format(self, key: str) -> bool:
        """Checks if key conforms to Google's key formats (new 'AQ.' or legacy 'AIza')."""
        return key.startswith("AQ.") or key.startswith("AIza") or key.startswith("test")

    async def solve(
        self, question: str, options: Optional[List[str]] = None
    ) -> Tuple[Optional[str], float, str]:
        """
        Solves a Pokémon trivia / captcha question using Google Gemini API.
        
        Returns:
            Tuple of (answer_text, confidence_score, explanation)
        """
        if not self.api_key:
            logger.error("Gemini API key is not configured in .env!")
            return None, 0.0, "API key missing"

        options_prompt = ""
        if options:
            options_prompt = (
                f"\nYou MUST choose the exact matching answer from these provided options: {json.dumps(options)}."
                "\nReturn the exact option string verbatim as 'answer'."
            )
        else:
            options_prompt = "\nProvide the shortest, direct factual answer (e.g. '4', 'Master Ball')."

        system_prompt = (
            "You are an infallible Pokémon game mechanic expert and competitive player. "
            "You are answering an anti-bot trivia verification question for a Pokémon game. "
            "You must be 100% accurate. A mistake will cause an instant ban."
            f"\nQuestion: {question}"
            f"{options_prompt}"
            "\n\nRespond ONLY with a valid JSON object in this format:"
            "\n{"
            '\n  "answer": "string containing the exact correct answer/option",'
            '\n  "confidence": 1.0,'
            '\n  "explanation": "brief reason for this answer"'
            "\n}"
        )

        payload = {
            "contents": [
                {
                    "parts": [{"text": system_prompt}]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json"
            }
        }

        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(self.endpoint, json=payload) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        logger.error("Gemini API error (status %d): %s", resp.status, err_text)
                        return None, 0.0, f"HTTP error {resp.status}"

                    data = await resp.json()
                    candidates = data.get("candidates", [])
                    if not candidates:
                        logger.error("Gemini returned empty candidates")
                        return None, 0.0, "No candidates returned"

                    content_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                    content_text = content_text.strip()
                    if content_text.startswith("```"):
                        content_text = re.sub(r"^```(?:json)?\s*", "", content_text)
                        content_text = re.sub(r"\s*```$", "", content_text)

                    parsed = json.loads(content_text)
                    answer = str(parsed.get("answer", "")).strip()
                    confidence = float(parsed.get("confidence", 0.0))
                    explanation = str(parsed.get("explanation", ""))

                    logger.info("Gemini solved: Q='%s' -> Answer='%s' (Confidence=%.2f, Reason='%s')",
                                question, answer, confidence, explanation)
                    return answer, confidence, explanation

        except aiohttp.ClientError as e:
            logger.error("Network error communicating with Gemini API: %s", e)
            return None, 0.0, f"Network error: {e}"
        except json.JSONDecodeError as e:
            logger.error("Failed to parse JSON from Gemini response: %s", e)
            return None, 0.0, f"JSON parse error: {e}"
        except Exception as e:
            logger.error("Unexpected error in GeminiSolver: %s", e)
            return None, 0.0, f"Unexpected error: {e}"

    async def validate_api_key(self) -> Tuple[bool, str]:
        """
        Validates the configured Gemini API key.
        Checks format and issues a lightweight ping to verify credentials.
        """
        if not self.api_key:
            return False, "GEMINI_API_KEY is not set in .env."

        if not self._is_valid_format(self.api_key):
            return False, (
                f"Invalid key format: '{self.api_key[:10]}...'\n"
                "Google Gemini API keys start with 'AQ.' (new 2026 format) or 'AIza' (legacy format).\n"
                "Please get a free API key at: https://aistudio.google.com/app/apikey"
            )

        payload = {
            "contents": [{"parts": [{"text": "ping"}]}]
        }
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
                async with session.post(self.endpoint, json=payload) as resp:
                    if resp.status == 200:
                        return True, "Gemini API key verified successfully."
                    err = await resp.text()
                    return False, f"Gemini API returned status {resp.status}: {err}"
        except Exception as e:
            return False, f"Network check failed: {e}"

    async def solve_pokemon_silhouette(
        self, image_bytes: bytes, mime_type: Optional[str] = None
    ) -> Tuple[Optional[str], float, str]:
        """
        Uses Gemini 2.5 Flash Vision to identify a Pokémon from a 'Who's that Pokémon?' silhouette image.
        
        Returns:
            Tuple of (pokemon_name, confidence_score, explanation)
        """
        if not self.api_key:
            logger.error("Gemini API key is not configured in .env!")
            return None, 0.0, "API key missing"

        if not self._is_valid_format(self.api_key):
            err_msg = (
                f"INVALID GEMINI KEY: '{self.api_key[:10]}...' does not start with 'AQ.' or 'AIza'. "
                "Get a valid free key at https://aistudio.google.com/app/apikey"
            )
            logger.critical(err_msg)
            return None, 0.0, err_msg

        # Auto-detect MIME type from magic bytes if not explicitly provided
        if not mime_type:
            if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                mime_type = "image/png"
            elif image_bytes.startswith(b"\xff\xd8"):
                mime_type = "image/jpeg"
            elif image_bytes.startswith(b"RIFF") and len(image_bytes) >= 12 and image_bytes[8:12] == b"WEBP":
                mime_type = "image/webp"
            else:
                mime_type = "image/jpeg"

        import base64
        b64_img = base64.b64encode(image_bytes).decode("utf-8")

        prompt = (
            "You are an infallible Pokémon master and visual recognition expert. "
            "Identify the Pokémon shown in this black silhouette from 'Who's that Pokémon?'. "
            "You must return the official English Pokémon name (e.g., 'Shellder', 'Pikachu', 'Seedot', 'Cascoon'). "
            "Do NOT include any extra words, symbols, or punctuation in the pokemon field.\n\n"
            "Respond ONLY with a valid JSON object in this exact schema:\n"
            "{\n"
            '  "pokemon": "ExactPokemonName",\n'
            '  "confidence": 1.0,\n'
            '  "reasoning": "brief visual rationale"\n'
            "}"
        )

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": prompt},
                        {
                            "inline_data": {
                                "mime_type": mime_type,
                                "data": b64_img
                            }
                        }
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0.0,
                "responseMimeType": "application/json"
            }
        }

        timeout = aiohttp.ClientTimeout(total=12)
        candidate_models = [self.model]
        for fb in ["gemini-3.1-flash-lite", "gemini-3.8-flash"]:
            if fb not in candidate_models:
                candidate_models.append(fb)

        last_error = "Unknown error"
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for model_name in candidate_models:
                    endpoint = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={self.api_key}"
                    async with session.post(endpoint, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            candidates = data.get("candidates", [])
                            if not candidates:
                                continue

                            content_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
                            content_text = content_text.strip()
                            if content_text.startswith("```"):
                                content_text = re.sub(r"^```(?:json)?\s*", "", content_text)
                                content_text = re.sub(r"\s*```$", "", content_text)

                            parsed = json.loads(content_text)
                            pokemon = str(parsed.get("pokemon", "")).strip()
                            confidence = float(parsed.get("confidence", 0.0))
                            reasoning = str(parsed.get("reasoning", ""))

                            logger.info("Gemini Vision (%s) identified silhouette: '%s' (Confidence=%.2f, Reason='%s')",
                                        model_name, pokemon, confidence, reasoning)
                            return pokemon, confidence, reasoning
                        else:
                            err_text = await resp.text()
                            last_error = f"HTTP error {resp.status}"
                            logger.warning("Gemini Vision model '%s' returned status %d. Trying fallback...", model_name, resp.status)

            return None, 0.0, last_error

        except Exception as e:
            logger.error("Error in solve_pokemon_silhouette: %s", e)
            return None, 0.0, f"Error: {e}"

