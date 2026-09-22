"""Приёмник вебхука GGSell о новой продаже (notification_settings.url на
каждом оффере).

ПОДТВЕРЖДЕНО НА ЖИВОМ ЗАКАЗЕ (22 сентября): метод POST, тело — JSON:

    {"id_i": 51308662, "id_d": 103170287, "amount": "1.0",
     "currency": "RUB", "email": "...", "date": "...", "ip": "...",
     "SHA256": "...", "is_my_product": true}

id_i = invoice_id (для get_order_info). id_d = item_id = Listing.ggsell_offer_id
(оба подтверждены сверкой с get_order_info на реальном заказе).

Подпись SHA256 — что именно хешируется, не известно, пока не проверяем
(см. TODO ниже).
"""

from __future__ import annotations

import json
import logging
import os

from dotenv import load_dotenv
from fastapi import APIRouter, Request

from app.clients.fazercards import FazerCardsClient
from app.clients.ggsell import GGSellV1Client
from app.db import SessionLocal
from app.orders.order_processor import OrderProcessingError, process_new_order
from app.pricing.calculator import PricingConfig

logger = logging.getLogger(__name__)

router = APIRouter()

load_dotenv()


@router.api_route("/webhooks/ggsell", methods=["GET", "POST"])
async def ggsell_webhook(request: Request):
    raw_body = await request.body()
    logger.info(
        "GGSell webhook: method=%s query_params=%s body=%r",
        request.method,
        dict(request.query_params),
        raw_body,
    )

    payload = dict(request.query_params)
    if raw_body:
        try:
            payload.update(json.loads(raw_body))
        except ValueError:
            logger.warning("GGSell webhook: не удалось распарсить JSON-тело: %r", raw_body)

    id_i = payload.get("id_i")
    if id_i is None:
        logger.warning("GGSell webhook пришёл без id_i — не могу определить заказ")
        return {"ok": True}  # отвечаем 200 в любом случае, чтобы GGSell не ретраил бесконечно

    # TODO: проверка подписи SHA256 — не реализована, не знаем, что именно
    # хешируется. Определить эмпирически, прежде чем полагаться на неё как
    # на защиту от поддельных вебхуков.

    logger.info("Новая продажа: id_i=%s id_d=%s amount=%s", id_i, payload.get("id_d"), payload.get("amount"))

    session = SessionLocal()
    try:
        with FazerCardsClient(
            api_key=os.getenv("FAZERCARDS_API_KEY"),
            base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2"),
        ) as fz_client, GGSellV1Client(
            seller_id=int(os.getenv("GGSELL_V1_SELLER_ID")),
            api_key=os.getenv("GGSELL_V1_API_KEY"),
            base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
        ) as ggsell_v1:
            pricing_config = PricingConfig.from_env()
            order = process_new_order(
                session, fz_client, ggsell_v1, pricing_config, invoice_id=str(id_i)
            )
            logger.info("Заказ %s обработан, статус=%s", id_i, order.status)
    except OrderProcessingError as e:
        logger.error("Заказ %s: ошибка обработки: %s", id_i, e)
    except Exception:
        logger.exception("Заказ %s: необработанная ошибка при обработке вебхука", id_i)
    finally:
        session.close()

    return {"ok": True}
