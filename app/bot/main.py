"""Точка входа бота администратора (roadmap 6.7): long polling Telegram.

Запуск: python -m app.bot.main (в docker-compose — сервис `bot`).
Алерты бот не шлёт — их отправляет тот процесс, где они возникли
(app/orders/notifications.py); бот отвечает на команды и кнопки.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

from dotenv import load_dotenv

from app.bot.handlers import BotApp
from app.clients.fazercards import FazerCardsClient
from app.clients.ggsell import GGSellV1Client, GGSellV2Client
from app.clients.telegram import TelegramClient, admin_chat_ids, bot_token
from app.db import SessionLocal

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

COMMANDS = [
    ("status", "Состояние системы"),
    ("pricer", "Авто-прайсер: курс, наценка, ВКЛ/ВЫКЛ"),
    ("orders", "Последние заказы"),
    ("manual", "Заказы на ручном разборе"),
    ("lot", "Найти лот: /lot <id или название>"),
    ("sync", "Обновить цены сейчас"),
    ("cancel", "Отменить ввод"),
]


class LiveServices:
    """Настоящие GGSell и FazerCards — клиенты создаются на каждый вызов."""

    def fz_balance(self) -> Optional[dict[str, Any]]:
        with FazerCardsClient(
            api_key=os.getenv("FAZERCARDS_API_KEY"),
            base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2"),
        ) as fz:
            return fz.get_balance()

    def send_to_buyer(self, invoice_id: int, text: str) -> None:
        with GGSellV1Client(
            seller_id=int(os.getenv("GGSELL_V1_SELLER_ID")),
            api_key=os.getenv("GGSELL_V1_API_KEY"),
            base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
        ) as v1:
            v1.create_message(invoice_id, text)

    def set_offer_active(self, offer_id: int, active: bool) -> Any:
        with GGSellV2Client(
            api_key=os.getenv("GGSELL_API_KEY"),
            base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
        ) as v2:
            return v2.batch_activate_offers([offer_id]) if active else v2.batch_pause_offers([offer_id])


def main() -> None:
    load_dotenv()
    token, admins = bot_token(), admin_chat_ids()
    if not token or not admins:
        # Не падаем в цикл рестартов контейнера — ждём, пока заполнят .env.
        logger.error("ADMIN_TELEGRAM_BOT_TOKEN / ADMIN_TELEGRAM_CHAT_ID не заданы в .env — бот не запущен")
        while True:
            time.sleep(3600)

    with TelegramClient(token) as tg:
        app = BotApp(tg=tg, services=LiveServices(), session_factory=SessionLocal, admin_ids=admins)
        try:
            tg.set_my_commands(COMMANDS)
        except Exception:  # noqa: BLE001
            logger.exception("Не удалось задать список команд бота")
        logger.info("Бот запущен, админов: %d", len(admins))

        offset: Optional[int] = None
        while True:
            try:
                updates = tg.get_updates(offset=offset, timeout=30)
            except Exception:  # noqa: BLE001 — сеть/Telegram недоступны: ждём и пробуем снова
                logger.exception("getUpdates не удался — повтор через 5 с")
                time.sleep(5)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                app.handle_update(update)


if __name__ == "__main__":
    main()
