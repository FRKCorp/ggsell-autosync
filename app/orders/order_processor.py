"""Обработка заказа от момента продажи на GGSell до выдачи покупателю.

Последовательность (см. docs/architecture-notes.md, раздел 3.3):
  1. Получить детали заказа у GGSell (V1 get_order_info).
  2. Найти Listing/Position по купленному офферу.
  3. Достать данные покупателя (invite_url/Player ID) из options[].user_data.
  4. Повторно проверить цену у FazerCards прямо перед списанием — если
     разошлась больше MAX_PRICE_DEVIATION_PERCENT, уходим в ручной режим,
     заказ не отправляется автоматически.
  5. Заказать у FazerCards (topup/giftcard/steam_gift — по source_type
     позиции), с ретраями и детерминированным Idempotency-Key.
  6. Сформировать сообщение из ответа поставщика.
  7. Отправить сообщение в чат заказа (create_message) — это и есть финал
     happy path; отдельного "закрытия" заказа на GGSell не существует
     (см. 3.6/3.8 в заметках — подтверждено поддержкой GGSell).

ПОДТВЕРЖДЕНО (Swagger V1, страницы Get order info / check unique code):
  - get_order_info: GET /api_sellers/api/purchase/info/:invoice_id,
    query token, header locale: ru.
  - check_unique_code: GET /api_sellers/api/purchases/unique-code/:unique_code —
    unique_code это НЕ invoice_id, отдельная строковая сущность.

НЕ ПОДТВЕРЖДЕНО ЭМПИРИЧЕСКИ:
  - Источник chat_id для create_message — задокументированный list_chats возвращает
    пустые записи (тот же паттерн, что и с delivery — реальный чат живёт в
    недокументированном внутреннем API с браузерной авторизацией). Ждём
    ответа поддержки GGSell, см. architecture-notes.md 3.13.
  - Формат успешного ответа order_topup/order_giftcard/order_steam_gift —
    приходит ли код сразу в ответе или нужен отдельный поллинг.
  Всё это помечено TODO в коде ниже — исправить когда придёт ответ поддержки
  или на первом реальном заказе с товаром.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsClient, FazerCardsError
from app.clients.ggsell import GGSellError, GGSellV1Client
from app.models.listing import Listing
from app.models.order import Order, OrderStatus
from app.models.position import Position, SourceType
from app.orders.notifications import notify_admin
from app.pricing.calculator import PricingConfig, check_price_deviation

logger = logging.getLogger(__name__)

MAX_FZ_ORDER_RETRIES = 3
FZ_ORDER_RETRY_DELAY_SECONDS = 5


class OrderProcessingError(Exception):
    """Заказ не может быть обработан из-за проблемы с данными (не с
    внешним API) — например, Listing не найден в БД."""


def extract_buyer_data(options: list[dict[str, Any]]) -> dict[str, str]:
    """options — из info_order.content.options: [{id, name, user_data,
    user_data_id}]. Возвращает {name: user_data} — плоский словарь для
    передачи в fields при заказе у FazerCards (там ожидается что-то вроде
    {"user_id": "..."})."""
    return {
        opt["name"]: opt["user_data"]
        for opt in options
        if opt.get("name") and opt.get("user_data") is not None
    }


def find_listing_by_offer_id(session: Session, ggsell_offer_id: int) -> Optional[Listing]:
    return session.scalar(select(Listing).where(Listing.ggsell_offer_id == ggsell_offer_id))


@dataclass
class OrderContext:
    """Всё, что нужно для обработки одного заказа — собирается из
    order_info + Listing/Position, дальше передаётся между шагами."""

    invoice_id: str
    listing: Listing
    position: Position
    buyer_data: dict[str, str]
    price_at_sale_rub: Decimal
    chat_id: Optional[int] = None  # TODO: подтвердить источник (см. docstring модуля)


def build_order_context(
    session: Session, order_info: dict[str, Any], invoice_id: str
) -> OrderContext:
    """order_info — content-часть ответа get_order_info (info_order.content).
    Подтверждено на реальном заказе (22 сентября): item_id действительно
    равен Listing.ggsell_offer_id.
    """
    offer_id = order_info["item_id"]
    listing = find_listing_by_offer_id(session, offer_id)
    if listing is None:
        raise OrderProcessingError(f"Listing с ggsell_offer_id={offer_id} не найден в БД")

    buyer_data = extract_buyer_data(order_info.get("options", []))

    return OrderContext(
        invoice_id=invoice_id,
        listing=listing,
        position=listing.position,
        buyer_data=buyer_data,
        price_at_sale_rub=listing.price_rub,
    )


def get_or_create_order(session: Session, ctx: OrderContext) -> Order:
    existing = session.scalar(select(Order).where(Order.ggsell_invoice_id == ctx.invoice_id))
    if existing:
        return existing
    order = Order(
        ggsell_invoice_id=ctx.invoice_id,
        listing_id=ctx.listing.id,
        status=OrderStatus.PENDING,
        buyer_data=ctx.buyer_data,
        price_at_sale_rub=ctx.price_at_sale_rub,
    )
    session.add(order)
    session.flush()
    return order


def verify_price_before_charge(
    fz_client: FazerCardsClient, position: Position, config: PricingConfig
) -> tuple[bool, Decimal]:
    """Повторно запрашивает актуальную цену у FazerCards прямо перед
    списанием и сверяет с ценой на момент последней синхронизации
    (position.last_known_price_usd). Возвращает (ok, live_price_usd).
    """
    if position.source_type == SourceType.TOPUP:
        data = fz_client.get_topup_offers(position.fz_category_id)
        offer = next(o for o in data["offers"] if o["offer_id"] == position.fz_offer_id)
        live_price = Decimal(offer["price_usd"])
    elif position.source_type == SourceType.GIFTCARD:
        data = fz_client.get_giftcard_offers(position.fz_category_id)
        offer = next(o for o in data["offers"] if o["card_id"] == position.fz_offer_id)
        live_price = Decimal(offer["price_usd"])
    elif position.source_type == SourceType.STEAM_GIFT:
        data = fz_client.get_steam_gift_offers(position.fz_appid)
        offer = next(o for o in data["offers"] if o["sub_id"] == position.fz_sub_id)
        region_price = next(r["price"] for r in offer["regions"] if r["region"] == position.region)
        live_price = Decimal(region_price)
    else:
        raise OrderProcessingError(f"Неизвестный source_type: {position.source_type}")

    check = check_price_deviation(position.last_known_price_usd, live_price, config)
    return check.within_threshold, live_price


def place_fz_order(
    fz_client: FazerCardsClient,
    position: Position,
    buyer_data: dict[str, str],
    idempotency_key: str,
) -> dict[str, Any]:
    """Заказывает товар у FazerCards, метод зависит от source_type.
    idempotency_key должен быть детерминированным (используем invoice_id)
    — чтобы повторная попытка после таймаута не задвоила заказ/списание.
    """
    if position.source_type == SourceType.TOPUP:
        return fz_client.order_topup(
            position.fz_category_id,
            position.fz_offer_id,
            buyer_data,
            idempotency_key=idempotency_key,
        )
    if position.source_type == SourceType.GIFTCARD:
        return fz_client.order_giftcard(
            position.fz_category_id,
            position.fz_offer_id,
            quantity=1,
            idempotency_key=idempotency_key,
        )
    if position.source_type == SourceType.STEAM_GIFT:
        invite_url = buyer_data.get("invite_url") or buyer_data.get("Invite URL")
        if not invite_url:
            raise OrderProcessingError("Не найден invite_url в данных покупателя для Steam-гифта")
        return fz_client.order_steam_gift(
            invite_url=invite_url,
            sub_id=position.fz_sub_id,
            app_id=position.fz_appid,
            region=position.region,
            idempotency_key=idempotency_key,
        )
    raise OrderProcessingError(f"Неизвестный source_type: {position.source_type}")


def format_delivery_message(fz_order_result: dict[str, Any]) -> str:
    """Формирует текст сообщения для покупателя из ответа FazerCards.

    TODO: точная структура успешного ответа order_* (где именно лежит
    код/данные для выдачи) не подтверждена на живом заказе — сейчас
    просто сериализуем то, что пришло, чтобы не потерять данные, пока не
    увидим реальный формат.
    """
    return f"Ваш заказ выполнен. Детали: {fz_order_result}"


def process_new_order(
    session: Session,
    fz_client: FazerCardsClient,
    ggsell_v1: GGSellV1Client,
    pricing_config: PricingConfig,
    invoice_id: str,
) -> Order:
    """Главная точка входа — вызывается из webhook-хендлера (или из
    поллинга last_sales, как запасного варианта) с invoice_id заказа.
    Идемпотентна на уровне БД: повторный вызов с тем же invoice_id для
    уже обработанного заказа не запускает обработку заново.
    """
    # TODO: путь get_order_info не подтверждён (см. GGSellV1Client.get_order_info).
    order_info_response = ggsell_v1.get_order_info(int(invoice_id))
    order_info = order_info_response["content"]

    ctx = build_order_context(session, order_info, invoice_id)
    order = get_or_create_order(session, ctx)

    if order.status != OrderStatus.PENDING:
        logger.info(
            "Заказ %s уже в статусе %s, пропускаю повторную обработку",
            invoice_id, order.status,
        )
        return order

    price_ok, live_price_usd = verify_price_before_charge(fz_client, ctx.position, pricing_config)
    if not price_ok:
        order.status = OrderStatus.PRICE_REJECTED
        order.error_message = (
            f"Цена разошлась: кэш={ctx.position.last_known_price_usd} USD, "
            f"живая={live_price_usd} USD, порог={pricing_config.max_price_deviation_percent}%"
        )
        session.commit()
        notify_admin(
            f"Заказ {invoice_id}: цена разошлась больше порога, отправлен в ручной режим. "
            f"{order.error_message}"
        )
        return order

    fz_result = None
    last_error: Optional[str] = None
    for attempt in range(1, MAX_FZ_ORDER_RETRIES + 1):
        try:
            fz_result = place_fz_order(
                fz_client, ctx.position, ctx.buyer_data, idempotency_key=invoice_id
            )
            break
        except FazerCardsError as e:
            last_error = f"FazerCards error {e.status_code}: {e.error}"
            logger.warning(
                "Попытка %d/%d заказа у FZ для invoice_id=%s не удалась: %s",
                attempt, MAX_FZ_ORDER_RETRIES, invoice_id, last_error,
            )
            if attempt < MAX_FZ_ORDER_RETRIES:
                time.sleep(FZ_ORDER_RETRY_DELAY_SECONDS)

    if fz_result is None:
        order.status = OrderStatus.MANUAL_REVIEW
        order.retry_count = MAX_FZ_ORDER_RETRIES
        order.error_message = last_error
        session.commit()
        notify_admin(
            f"Заказ {invoice_id}: FazerCards не ответил после {MAX_FZ_ORDER_RETRIES} попыток. "
            f"{last_error}. Требуется ручная обработка."
        )
        return order

    order.status = OrderStatus.ORDERED_UPSTREAM
    order.fz_order_id = str(fz_result.get("order_id") or fz_result.get("id") or "")
    session.commit()

    message = format_delivery_message(fz_result)

    if ctx.chat_id is None:
        order.status = OrderStatus.MANUAL_REVIEW
        order.error_message = "Товар получен у FZ, но не найден chat_id для отправки покупателю"
        session.commit()
        notify_admin(
            f"Заказ {invoice_id}: товар получен у поставщика, но не удалось определить чат для "
            f"выдачи покупателю. Выдать вручную! Данные: {message}"
        )
        return order

    try:
        ggsell_v1.create_message(ctx.chat_id, message)
    except GGSellError as e:
        order.status = OrderStatus.MANUAL_REVIEW
        order.error_message = f"Товар получен у FZ, но не отправлен в чат: {e.status_code} {e.payload}"
        session.commit()
        notify_admin(
            f"Заказ {invoice_id}: товар получен, но отправка в чат покупателю не удалась. "
            f"Выдать вручную! {order.error_message}"
        )
        return order

    order.status = OrderStatus.DELIVERED
    order.delivered_message = message
    session.commit()
    logger.info("Заказ %s успешно обработан и выдан покупателю", invoice_id)
    return order
