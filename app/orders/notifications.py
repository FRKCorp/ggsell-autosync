"""Уведомления администратору о заказах, требующих ручного вмешательства.

Сейчас — только логирование. Телеграм-бот (ADMIN_TELEGRAM_BOT_TOKEN/
ADMIN_TELEGRAM_CHAT_ID из .env) подключим отдельно при деплое — сама
механика уведомления не блокирует остальную логику orders/, поэтому не
тянем её сюда сейчас.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def notify_admin(message: str) -> None:
    logger.warning("ADMIN ALERT: %s", message)
