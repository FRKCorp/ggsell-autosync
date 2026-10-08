"""Уведомления администратору: заказы на ручном разборе,
пропажи у FZ, просевшая маржа, сбои синхронизации.

Всегда пишется в лог; если в .env заданы ADMIN_TELEGRAM_BOT_TOKEN и
ADMIN_TELEGRAM_CHAT_ID — ещё и в Telegram (всем chat_id из списка).
Сбой Telegram никогда не роняет вызывающий код — обработку заказа,
синхронизацию: алерт остаётся в логе.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from app.clients.telegram import TelegramClient, admin_chat_ids, bot_token

logger = logging.getLogger(__name__)

# Подсказка под алертом о заказе: ответ на такое сообщение бот отправит
# покупателю в чат заказа (app/bot/handlers.py).
REPLY_HINT = "↩️ Ответьте на это сообщение, чтобы написать покупателю."


# Shared-прокси для Telegram периодически отказывает в соединении на
# секунды-минуты (стейдж, 05.10: 153 отказа за двое суток) — пробуем ещё раз.
SEND_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (3, 10)


def notify_admin(message: str, client: Optional[TelegramClient] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
    logger.warning("ADMIN ALERT: %s", message)
    token, chat_ids = bot_token(), admin_chat_ids()
    if not token or not chat_ids:
        return
    own_client = client is None
    client = client or TelegramClient(token, timeout=10)
    try:
        for chat_id in chat_ids:
            for attempt in range(1, SEND_ATTEMPTS + 1):
                try:
                    client.send_message(chat_id, message)
                    break
                except Exception as e:  # noqa: BLE001 — алерт не должен ронять обработку заказа
                    if attempt == SEND_ATTEMPTS:
                        logger.error("Алерт в Telegram (chat %s) не отправлен после %d попыток (%r) — "
                                     "остался только в логе", chat_id, SEND_ATTEMPTS, e)
                    else:
                        sleep(RETRY_DELAYS_SECONDS[attempt - 1])
    finally:
        if own_client:
            client.close()
