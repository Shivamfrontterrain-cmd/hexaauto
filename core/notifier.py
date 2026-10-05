import logging
from typing import List, Optional
import aiohttp

logger = logging.getLogger("autohexa.notifier")

class EmergencyNotifier:
    """Tier 3: Sends urgent alerts to the owner when an unverified check occurs."""

    def __init__(self, user_id: int, bot_token: Optional[str] = None):
        self.user_id = user_id
        self.bot_token = bot_token

    async def notify_unknown_check(
        self,
        account_name: str,
        question: str,
        options: Optional[List[str]] = None,
        reason: str = "Unrecognized check / Low confidence"
    ):
        """Dispatches an urgent alert to the user's personal Telegram."""
        opts_str = "\n".join([f"• {opt}" for opt in options]) if options else "None (text input required)"
        alert_text = (
            "🚨 <b>EMERGENCY BOT-CHECK ALERT!</b> 🚨\n\n"
            f"<b>Account:</b> <code>{account_name}</code>\n"
            f"<b>Reason:</b> {reason}\n\n"
            f"<b>Question:</b>\n<i>{question}</i>\n\n"
            f"<b>Detected Buttons / Options:</b>\n{opts_str}\n\n"
            "⚠️ <b>Action:</b> Automation has been <b>PAUSED</b> on this account to prevent a ban. "
            "Please check the chat manually immediately!"
        )

        logger.critical("\n" + "="*50 + "\n" + alert_text + "\n" + "="*50)

        # If a Telegram Bot Token is configured, push directly via Telegram Bot API
        if self.bot_token and self.user_id:
            api_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.user_id,
                "text": alert_text,
                "parse_mode": "HTML"
            }
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(api_url, json=payload) as resp:
                        if resp.status == 200:
                            logger.info("Successfully pushed emergency alert to Telegram user %d", self.user_id)
                        else:
                            logger.error("Failed to send alert via bot token: %s", await resp.text())
            except Exception as e:
                logger.error("Error sending emergency Telegram alert: %s", e)
