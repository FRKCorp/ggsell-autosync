"""Приёмник вебхука GGSell о новой продаже (notification_settings.url на
каждом оффере). Формат подтверждён частично — из формы настройки уведомлений
в личном кабинете GGSell известны поля для GET-запроса:

    id_i, id_d, amount, curr, date, email, sha256, ip, isMyProduct

Не подтверждено эмпирически:
- Совпадает ли набор полей для POST (тело JSON? form-data? или те же
  query-параметры, просто с методом POST)? Принимаем оба варианта ниже —
  и query, и JSON-тело — чтобы не потерять данные независимо от того, как
  GGSell реально их пришлёт.
- Что именно хешируется в sha256 (подпись для проверки подлинности запроса).
  Пока только логируем значение, не проверяем — see TODO ниже.

Как только реальный вебхук прилетит (см. инструкцию в конце файла про
ngrok) — сверить фактические поля с этим списком и поправить.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Query, Request

logger = logging.getLogger(__name__)

router = APIRouter()


@router.api_route("/webhooks/ggsell", methods=["GET", "POST"])
async def ggsell_webhook(
    request: Request,
    id_i: Optional[str] = Query(None),
    id_d: Optional[str] = Query(None),
    amount: Optional[str] = Query(None),
    curr: Optional[str] = Query(None),
    date: Optional[str] = Query(None),
    email: Optional[str] = Query(None),
    sha256: Optional[str] = Query(None),
    ip: Optional[str] = Query(None),
    isMyProduct: Optional[str] = Query(None),
):
    # Логируем вообще всё сырьё — метод, query-параметры, тело — чтобы на
    # первом же реальном вызове увидеть точный формат, а не гадать заранее.
    raw_body = await request.body()
    logger.info(
        "GGSell webhook: method=%s query_params=%s body=%r",
        request.method,
        dict(request.query_params),
        raw_body,
    )

    # TODO: проверка подписи sha256 — не реализована, т.к. не знаем, что
    # именно поставщик хеширует (api_key+id_i? shared secret+timestamp?).
    # Определить эмпирически на первом реальном вызове и сверить с
    # несколькими гипотезами, прежде чем полагаться на неё как на защиту.

    if id_i is None:
        logger.warning("GGSell webhook пришёл без id_i — не могу определить заказ")
        return {"ok": True}  # отвечаем 200 в любом случае, чтобы GGSell не ретраил бесконечно

    # TODO: здесь будет вызов app.orders.order_processor.process_new_order(id_i, ...)
    # — сам процессор ещё не написан, это следующий шаг.
    logger.info("Новая продажа: id_i=%s amount=%s curr=%s email=%s", id_i, amount, curr, email)

    return {"ok": True}
