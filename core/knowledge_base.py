import json
import re
import asyncio
import logging
from pathlib import Path
from typing import Optional, Dict

logger = logging.getLogger("autohexa.knowledge_base")

class KnowledgeBase:
    """Tier 1: Deterministic local question-answer database."""

    def __init__(self, filepath: Path):
        self.filepath = filepath
        self._lock = asyncio.Lock()
        self._data: Dict[str, str] = {}
        self._load()

    def _normalize(self, text: str) -> str:
        """Strip punctuation and lowercase text for robust matching."""
        text = text.lower()
        text = re.sub(r"[^\w\s]", "", text)
        return " ".join(text.split())

    def _load(self):
        if not self.filepath.exists():
            self._data = {}
            return
        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                raw_data = json.load(f)
                self._data = {self._normalize(k): v for k, v in raw_data.items()}
            logger.info("Loaded %d verified questions into KnowledgeBase", len(self._data))
        except Exception as e:
            logger.error("Failed to load knowledge base from %s: %s", self.filepath, e)
            self._data = {}

    async def find_answer(self, question: str) -> Optional[str]:
        """Search for an answer in the local database."""
        normalized_q = self._normalize(question)

        # 1. Exact match
        if normalized_q in self._data:
            return self._data[normalized_q]

        # 2. Substring or key pattern match
        for key, answer in self._data.items():
            if key in normalized_q or normalized_q in key:
                return answer

        return None

    async def save_answer(self, question: str, answer: str):
        """Save a newly verified question-answer pair."""
        normalized_q = self._normalize(question)
        async with self._lock:
            self._data[normalized_q] = answer
            try:
                with open(self.filepath, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                logger.info("Saved new question to KnowledgeBase: '%s' -> '%s'", question, answer)
            except Exception as e:
                logger.error("Failed to save knowledge base: %s", e)
