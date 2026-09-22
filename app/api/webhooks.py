"""Приёмник вебхука GGSell о новой продаже (notification_settings.url на
каждом оффере).

ПОДТВЕРЖДЕНО НА ЖИВОМ ЗАКАЗЕ (первый реальный вебхук, 22 сентября):
метод POST, тело — JSON (НЕ query-параметры, как можно было подумать из
описания в личном кабинете):

    {"id_i": 51308662, "id_d": 103170287, "amount": "1.0",
     "currency": "RUB", "email": "...", "date": "...", "ip": "...",
     "SHA256": "...", "is_my_product": true}

Имена полей отличаются от текста в личном кабинете GGSell (там было
curr/sha256/isMyProduct) — реальные имена: currency, SHA256, is_my_product.

id_i совпадает с номером заказа, который виден покупателю ("Заказ №
51308662") — это и есть invoice_id для get_order_info. Значение id_d пока
не подтверждено (предположительно offer_id) — сверить через get_order_info.

Подпись SHA256 — что именно хешируется, всё ещё не известно (см. TODO
ниже), пока не проверяем.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter()


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
            import json

            payload.update(json.loads(raw_body))
        except ValueError:
            logger.warning("GGSell webhook: не удалось распарсить JSON-тело: %r", raw_body)

    id_i = payload.get("id_i")
    if id_i is None:
        logger.warning("GGSell webhook пришёл без id_i — не могу определить заказ")
        return {"ok": True}  # отвечаем 200 в любом случае, чтобы GGSell не ретраил бесконечно

    # TODO: проверка подписи SHA256 — не реализована, не знаем, что именно
    # хешируется. Определить эмпирически (перебрать гипотезы: api_key+id_i?
    # api_key+id_i+amount? и т.д.) прежде чем полагаться на неё как на защиту.

    logger.info(
        "Новая продажа: id_i=%s id_d=%s amount=%s currency=%s email=%s",
        id_i, payload.get("id_d"), payload.get("amount"), payload.get("currency"),
        payload.get("email"),
    )

    # TODO: здесь будет вызов app.orders.order_processor.process_new_order(
    #     session, fz_client, ggsell_v1, pricing_config, invoice_id=str(id_i)
    # ) — сначала нужно подтвердить соответствие id_d <-> Listing.ggsell_offer_id
    # через get_order_info, см. scripts/inspect_order_info.py.

    return {"ok": True}
