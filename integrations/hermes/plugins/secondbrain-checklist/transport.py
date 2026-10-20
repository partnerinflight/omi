"""Telegram transports; sending only, never polling."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


class AmbiguousSendError(RuntimeError):
    """The caller cannot prove whether Telegram accepted a send."""


@dataclass(frozen=True)
class SendReceipt:
    chat_id: int
    message_id: int


class TelegramSender:
    """Create a one-shot PTB Bot for each send; never poll or cross event loops."""

    def preflight(self) -> None:
        if not os.environ.get("TELEGRAM_BOT_TOKEN", "").strip():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not available")

    async def send(self, *, chat_id: int, text: str, reply_markup: Any) -> SendReceipt:
        try:
            from telegram import Bot

            token = os.environ["TELEGRAM_BOT_TOKEN"]
            async with Bot(token=token) as bot:
                message = await bot.send_message(
                    chat_id=chat_id, text=text, reply_markup=reply_markup
                )
        except Exception as exc:
            # A timeout/disconnect may happen after Telegram accepted the request. The
            # reserved request id remains non-retryable until a human reconciles it.
            raise AmbiguousSendError("Telegram send outcome is ambiguous") from exc
        return SendReceipt(chat_id=int(message.chat_id), message_id=int(message.message_id))
