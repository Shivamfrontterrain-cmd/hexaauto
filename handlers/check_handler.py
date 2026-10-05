import asyncio
import logging
import random
import re
from typing import Dict, List, Optional
from telethon import TelegramClient
from telethon.tl.custom.message import Message

from core.knowledge_base import KnowledgeBase
from core.gemini_solver import GeminiSolver
from core.notifier import EmergencyNotifier
import config

logger = logging.getLogger("autohexa.check_handler")

CHECK_TRIGGER_PHRASES = [
    "quick check",
    "just a quick check",
    "bot check",
    "verification",
    "anti-bot",
    "are you a human",
    "answer within",
    "prove you are",
    "solve this"
]

class CheckHandler:
    """Detects, validates, and accurately resolves anti-bot verification checks."""

    def __init__(
        self,
        knowledge_base: KnowledgeBase,
        gemini_solver: GeminiSolver,
        notifier: EmergencyNotifier
    ):
        self.kb = knowledge_base
        self.gemini = gemini_solver
        self.notifier = notifier
        self.paused_accounts = set()

    def is_check_message(self, message: Message) -> bool:
        """Determines whether a message is an anti-bot check/captcha."""
        text = (message.raw_text or "").lower()
        if any(phrase in text for phrase in CHECK_TRIGGER_PHRASES):
            return True
        return False

    def extract_question(self, text: str) -> str:
        """Strips header phrases to isolate the core question."""
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        filtered = []
        for line in lines:
            line_lower = line.lower()
            if any(phrase in line_lower for phrase in ["just a quick check", "quick check", "answer within", "time left"]):
                continue
            filtered.append(line)
        return " ".join(filtered) if filtered else text.strip()

    async def handle_check(self, client: TelegramClient, account_name: str, message: Message) -> bool:
        """
        Executes the 3-Tier verification pipeline:
        Tier 1: Local KnowledgeBase
        Tier 2: Gemini 2.5 Flash with Temperature 0.0 & JSON schema
        Tier 3: Emergency Human-in-the-Loop Alert & Pause
        """
        raw_text = message.raw_text or ""
        question = self.extract_question(raw_text)
        logger.warning("[%s] Anti-bot check detected! Question: '%s'", account_name, question)

        # Collect inline keyboard buttons if present
        button_map: Dict[str, any] = {}
        if message.buttons:
            for row in message.buttons:
                for btn in row:
                    label = btn.text.strip()
                    button_map[label] = btn

        options = list(button_map.keys()) if button_map else None
        target_answer: Optional[str] = None
        source: str = ""

        # --- Tier 1: Local Deterministic Database ---
        cached_answer = await self.kb.find_answer(question)
        if cached_answer:
            target_answer = cached_answer
            source = "Tier 1 (KnowledgeBase)"
            logger.info("[%s] Resolved via %s: '%s'", account_name, source, target_answer)

        # --- Tier 2: Gemini 2.5 Flash Solver ---
        if not target_answer:
            logger.info("[%s] Not in local cache. Querying Tier 2 (Gemini API)...", account_name)
            gemini_ans, confidence, reason = await self.gemini.solve(question, options)
            if gemini_ans and confidence >= 0.90:
                target_answer = gemini_ans
                source = f"Tier 2 (Gemini AI, conf: {confidence:.2f})"
                logger.info("[%s] Resolved via %s: '%s' (Reason: %s)",
                            account_name, source, target_answer, reason)
                # Save to Tier 1 local cache for future instant hits
                await self.kb.save_answer(question, target_answer)
            else:
                logger.warning("[%s] Gemini failed or low confidence: ans=%s, conf=%s, reason=%s",
                               account_name, gemini_ans, confidence, reason)

        # --- Tier 3: Emergency Human-in-the-Loop Fallback ---
        if not target_answer:
            self.paused_accounts.add(account_name)
            await self.notifier.notify_unknown_check(
                account_name=account_name,
                question=question,
                options=options,
                reason="Unresolved by both local DB and Gemini AI."
            )
            return False

        # --- Simulated Human Reaction Delay ---
        delay = random.uniform(config.MIN_REACTION_DELAY, config.MAX_REACTION_DELAY)
        logger.info("[%s] Human jitter delay: waiting %.2f seconds before answering...", account_name, delay)
        await asyncio.sleep(delay)

        # --- Dispatch Answer (Button click vs. Text reply) ---
        try:
            if button_map:
                # Find matching button
                chosen_button = None
                target_lower = target_answer.lower()
                
                # 1. Exact match
                for label, btn in button_map.items():
                    if label.lower() == target_lower:
                        chosen_button = btn
                        break

                # 2. Substring/fuzzy match
                if not chosen_button:
                    for label, btn in button_map.items():
                        if target_lower in label.lower() or label.lower() in target_lower:
                            chosen_button = btn
                            break

                if chosen_button:
                    logger.info("[%s] Clicking button: '%s'", account_name, chosen_button.text)
                    await chosen_button.click()
                    logger.info("[%s] Check successfully answered via button click!", account_name)
                    return True
                else:
                    logger.error("[%s] Matched answer '%s' did not match any available buttons: %s",
                                 account_name, target_answer, list(button_map.keys()))
                    self.paused_accounts.add(account_name)
                    await self.notifier.notify_unknown_check(
                        account_name=account_name,
                        question=question,
                        options=options,
                        reason=f"Answer '{target_answer}' did not match any button."
                    )
                    return False
            else:
                # Text reply
                logger.info("[%s] Sending reply text: '%s'", account_name, target_answer)
                await message.reply(target_answer)
                logger.info("[%s] Check successfully answered via text message!", account_name)
                return True

        except Exception as e:
            logger.error("[%s] Exception while dispatching check answer: %s", account_name, e)
            return False
