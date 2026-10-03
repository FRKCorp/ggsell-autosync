"""Уведомления администратору (roadmap 6.2): заказы на ручном разборе,
пропажи у FZ, просевшая маржа, сбои синхронизации.

Всегда пишется в лог; если в .env заданы ADMIN_TELEGRAM_BOT_TOKEN и
ADMIN_TELEGRAM_CHAT_ID — ещё и в Telegram (всем chat_id из списка).
Сбой Telegram никогда не роняет вызывающий код — обработку заказа,
синхронизацию: алерт остаётся в логе.
"""

from __future__ import annotations

import logging
from typing import Optional

from app.clients.telegram import TelegramClient, admin_chat_ids, bot_token

logger = logging.getLogger(__name__)

# Подсказка под алертом о заказе: ответ на такое сообщение бот отправит
# покупателю в чат заказа (app/bot/handlers.py, roadmap 6.3).
REPLY_HINT = "↩️ Ответьте на это сообщение, чтобы написать покупателю."


def notify_admin(message: str, client: Optional[TelegramClient] = None) -> None:
    logger.warning("ADMIN ALERT: %s", message)
    token, chat_ids = bot_token(), admin_chat_ids()
    if not token or not chat_ids:
        return
    own_client = client is None
    try:
        client = client or TelegramClient(token, timeout=10)
        for chat_id in chat_ids:
            client.send_message(chat_id, message)
    except Exception:  # noqa: BLE001 — алерт не должен ронять обработку заказа
        logger.exception("Алерт в Telegram не отправлен — остался только в логе")
    finally:
        if own_client and client is not None:
            client.close()
