"""Приёмник вебхука GGSell о новой продаже (notification_settings.url на
каждом оффере).

ПОДТВЕРЖДЕНО НА ЖИВЫХ ЗАКАЗАХ (22.09 и 01.10): метод POST, тело — JSON:

    {"id_i": 53081129, "id_d": 103310519, "amount": "10.0",
     "currency": "RUB", "email": "...", "date": "...", "ip": "...",
     "SHA256": "...", "is_my_product": true}

id_i = invoice_id (для get_order_info). id_d = item_id = Listing.ggsell_offer_id.

Обработка — в фоне (roadmap 5.4): GGSell сразу получает 200, заказ у FZ
(с повторами и паузами) не держит соединение и не блокирует сервер.

Подпись SHA256 не проверяем: формулу подобрать не удалось (notes 6.17),
секрета у нас нет. Защита (roadmap 5.6) — сверка через get_order_info по
нашему токену: заказ должен быть на наш лот, совпадать с id_d и быть
оплаченным (order_processor.build_order_context). Поддельный вебхук может
лишь запустить обработку настоящего оплаченного заказа — а она идемпотентна.
Потерянный вебхук подбирает страховочная джоба (app/orders/jobs.py, 5.7).
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from dotenv import load_dotenv
from fastapi import APIRouter, BackgroundTasks, Request

from app.clients.ggsell import GGSellError
from app.db import SessionLocal
from app.orders.jobs import order_clients
from app.orders.order_processor import OrderProcessingError, process_new_order
from app.pricing.calculator import PricingConfig

logger = logging.getLogger(__name__)

router = APIRouter()

load_dotenv()


def process_order_in_background(invoice_id: str, offer_id: Optional[int]) -> None:
    """Полная обработка заказа — в пуле потоков FastAPI, после ответа GGSell."""
    session = SessionLocal()
    try:
        with order_clients() as (fz_client, ggsell_v1):
            order = process_new_order(
                session, fz_client, ggsell_v1, PricingConfig.from_env(),
                invoice_id=invoice_id, expected_offer_id=offer_id,
            )
        logger.info("Заказ %s обработан, статус=%s", invoice_id, order.status)
    except (OrderProcessingError, GGSellError) as e:
        session.rollback()
        logger.error("Заказ %s: ошибка обработки: %s", invoice_id, e)
    except Exception:
        session.rollback()
        logger.exception("Заказ %s: необработанная ошибка при обработке", invoice_id)
    finally:
        session.close()


@router.api_route("/webhooks/ggsell", methods=["GET", "POST"])
async def ggsell_webhook(request: Request, background_tasks: BackgroundTasks):
    raw_body = await request.body()
    logger.info(
        "GGSell webhook: method=%s query_params=%s body=%r",
        request.method, dict(request.query_params), raw_body,
    )

    payload = dict(request.query_params)
    if raw_body:
        try:
            payload.update(json.loads(raw_body))
        except ValueError:
            logger.warning("GGSell webhook: не удалось распарсить JSON-тело: %r", raw_body)

    id_i = payload.get("id_i")
    if id_i is None or not str(id_i).isdigit():
        logger.warning("GGSell webhook без корректного id_i — не могу определить заказ")
        return {"ok": True}  # 200 в любом случае, чтобы GGSell не ретраил бесконечно

    id_d = payload.get("id_d")
    offer_id = int(id_d) if id_d is not None and str(id_d).isdigit() else None
    logger.info("Новая продажа: id_i=%s id_d=%s amount=%s", id_i, id_d, payload.get("amount"))
    background_tasks.add_task(process_order_in_background, str(id_i), offer_id)
    return {"ok": True}
