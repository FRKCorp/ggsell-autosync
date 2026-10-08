"""Обработка заказа от момента продажи на GGSell до выдачи покупателю.

Последовательность (см. docs/architecture-notes.md, 3.3 и 6.16–6.17):
  1. Регистрация (register_order): get_order_info по номеру заказа, сверка
     с вебхуком (наш товар, оплачен — защита от поддельных вебхуков,
     roadmap 5.6), запись Order со статусом PENDING: данные покупателя,
     реально оплаченная сумма и выплата продавцу (roadmap 5.8).
  2. Захват (claim_order): атомарный переход PENDING → PROCESSING — заказ
     обрабатывает ровно один обработчик, даже если вебхуки пришли дважды
     или одновременно сработала страховочная джоба (roadmap 5.5).
  3. Перепроверка цены у FZ прямо перед закупкой (порог
     MAX_PRICE_DEVIATION_PERCENT, отклонение пишется в заказ).
  4. Для топапа — перевод данных покупателя из названий опций GGSell в
     ключи и значения FZ (app/offers/options.py).
  5. Заказ у FZ с Idempotency-Key = номер заказа GGSell (повтор возвращает
     тот же заказ FZ, без второго списания). Повторяются только сбои сети,
     5xx и исчерпанный лимит 429; остальные отказы FZ (400 «Insufficient
     balance» и т.п.) — сразу ручной разбор с текстом ответа (roadmap 5.2).
  6. FZ асинхронный (docs FZ): заказ создаётся в processing и потом
     становится completed / failed / refund. completed сразу — выдаём;
     processing — ORDERED_UPSTREAM, результат забирает poll_upstream_orders
     (джоба в scheduler) с таймаутом.
  7. Выдача: сообщение в чат заказа (create_message(invoice_id)) — коды карт
     или «пополнение выполнено» (app/orders/delivery.py, roadmap 5.3).
     Отдельного «закрытия» заказа в API GGSell нет; что GGSell считает
     обработкой заказа (резерв 12 ч) — открытый вопрос roadmap 5.0.

Точка входа для вебхука и страховочной джобы — process_new_order (полный
путь); для опроса FZ — poll_upstream_orders.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsClient, FazerCardsError, FazerCardsRateLimitError
from app.clients.ggsell import GGSellError, GGSellV1Client
from app.models.listing import Listing
from app.models.order import Order, OrderStatus
from app.models.position import Position, SourceType, is_unit_priced
from app.offers.options import BuyerDataError, buyer_fields_for, map_buyer_data_to_fz_fields
from app.orders.delivery import format_delivery_message
from app.orders.notifications import REPLY_HINT, notify_admin
from app.pricing.calculator import PricingConfig, check_price_deviation

logger = logging.getLogger(__name__)

MAX_FZ_ORDER_RETRIES = 3
FZ_ORDER_RETRY_DELAY_SECONDS = 5
# Сколько ждать, пока FZ выполнит заказ, прежде чем звать человека.
FZ_ORDER_TIMEOUT = timedelta(minutes=30)

# Статусы заказа FZ (docs FZ: processing → completed / failed / refund).
FZ_COMPLETED = "completed"
FZ_PROCESSING = "processing"
FZ_FINAL_FAILURES = ("failed", "refund")

# Покупки Telegram у FZ без гарантии Idempotency-Key (документация FZ, 7.1):
# после сбоя сети/5xx заказ мог пройти — повтор может купить второй раз.
NO_RETRY_SOURCE_TYPES = (SourceType.TELEGRAM_STARS, SourceType.TELEGRAM_PREMIUM)


class OrderProcessingError(Exception):
    """Заказ не может быть обработан из-за проблемы с данными (не с
    внешним API) — например, Listing не найден в БД или вебхук не
    сходится с заказом GGSell."""


def extract_buyer_data(options: list[dict[str, Any]]) -> dict[str, str]:
    """options — из info_order.content.options: [{id, name, user_data,
    user_data_id}]. Возвращает {название опции: что ввёл/выбрал покупатель}.
    Для radio_button GGSell присылает подпись варианта (пилот 01.10:
    {"Сервер": "asia"})."""
    return {
        opt["name"]: opt["user_data"]
        for opt in options
        if opt.get("name") and opt.get("user_data") is not None
    }


def find_listing_by_offer_id(session: Session, ggsell_offer_id: int) -> Optional[Listing]:
    return session.scalar(select(Listing).where(Listing.ggsell_offer_id == ggsell_offer_id))


def _decimal(value: Any) -> Optional[Decimal]:
    return Decimal(str(value)) if value is not None else None


# ----------------------------------------------------------------------
# 1. Регистрация заказа
# ----------------------------------------------------------------------


@dataclass
class OrderContext:
    """Всё, что нужно для обработки одного заказа — собирается из
    order_info + Listing/Position, дальше передаётся между шагами."""

    invoice_id: str
    listing: Listing
    position: Position
    buyer_data: dict[str, str]
    price_at_sale_rub: Decimal
    seller_payout_rub: Optional[Decimal] = None
    # Сколько единиц купили — у лотов с ценой за единицу (калькулятор GGSell:
    # сумма пополнения Steam, число звёзд), иначе 1.
    quantity: int = 1


def order_quantity(order_info: dict[str, Any], position: Position) -> int:
    """cnt_goods из get_order_info («100.0») — количество единиц, которое
    выбрал покупатель в калькуляторе. У обычных лотов — всегда 1."""
    if not is_unit_priced(position.source_type):
        return 1
    try:
        return int(Decimal(str(order_info.get("cnt_goods") or 1)))
    except ArithmeticError as e:
        raise OrderProcessingError(f"Не разобрать количество cnt_goods={order_info.get('cnt_goods')!r}") from e


def build_order_context(
    session: Session,
    order_info: dict[str, Any],
    invoice_id: str,
    expected_offer_id: Optional[int] = None,
) -> OrderContext:
    """order_info — content-часть ответа get_order_info. Сверка (roadmap 5.6):
    заказ должен быть на наш лот, совпадать с id_d вебхука и быть оплаченным.
    Подписать вебхук у нас нечем (формула SHA256 GGSell неизвестна, notes
    6.17), поэтому правда — то, что отдаёт API GGSell по нашему токену."""
    offer_id = order_info["item_id"]
    if expected_offer_id is not None and int(expected_offer_id) != int(offer_id):
        raise OrderProcessingError(
            f"Заказ {invoice_id}: в вебхуке товар {expected_offer_id}, а у GGSell — {offer_id}"
        )
    if not order_info.get("date_pay"):
        raise OrderProcessingError(f"Заказ {invoice_id} не оплачен (нет date_pay)")
    listing = find_listing_by_offer_id(session, offer_id)
    if listing is None:
        raise OrderProcessingError(f"Listing с ggsell_offer_id={offer_id} не найден в БД")

    return OrderContext(
        invoice_id=invoice_id,
        listing=listing,
        position=listing.position,
        buyer_data=extract_buyer_data(order_info.get("options", [])),
        # Реально оплаченная сумма, а не цена лота (пилот 01.10: лот 67 ₽,
        # оплачено 10 ₽). Нет amount — цена лота как запасной вариант.
        price_at_sale_rub=_decimal(order_info.get("amount")) or listing.price_rub,
        seller_payout_rub=_decimal(order_info.get("profit")),
        quantity=order_quantity(order_info, listing.position),
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
        seller_payout_rub=ctx.seller_payout_rub,
        quantity=ctx.quantity,
    )
    session.add(order)
    try:
        session.commit()
    except IntegrityError:
        # Второй вебхук по тому же заказу успел записать его раньше (5.5).
        session.rollback()
        return session.scalar(select(Order).where(Order.ggsell_invoice_id == ctx.invoice_id))
    return order


def register_order(
    session: Session,
    ggsell_v1: GGSellV1Client,
    invoice_id: str,
    expected_offer_id: Optional[int] = None,
) -> tuple[Order, OrderContext]:
    order_info = ggsell_v1.get_order_info(int(invoice_id))["content"]
    ctx = build_order_context(session, order_info, invoice_id, expected_offer_id)
    return get_or_create_order(session, ctx), ctx


# ----------------------------------------------------------------------
# 2. Захват
# ----------------------------------------------------------------------


def claim_order(session: Session, order: Order) -> bool:
    """Атомарно PENDING → PROCESSING. False — заказ уже взял кто-то другой."""
    result = session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == OrderStatus.PENDING)
        .values(status=OrderStatus.PROCESSING)
    )
    session.commit()
    session.refresh(order)
    return result.rowcount == 1


def _to_manual_review(session: Session, order: Order, reason: str, alert: str) -> Order:
    order.status = OrderStatus.MANUAL_REVIEW
    order.error_message = reason
    session.commit()
    notify_admin(f"{alert}\n\n{REPLY_HINT}")
    return order


# ----------------------------------------------------------------------
# 3–5. Цена, данные покупателя, заказ у FZ
# ----------------------------------------------------------------------


def verify_price_before_charge(
    fz_client: FazerCardsClient, position: Position, config: PricingConfig
) -> tuple[bool, Decimal, list[dict[str, Any]], Decimal]:
    """Повторно запрашивает актуальную цену у FazerCards прямо перед
    списанием и сверяет с ценой на момент последней синхронизации
    (position.last_known_price_usd). Возвращает (ok, live_price_usd,
    fz_fields, deviation_percent) — fields из того же ответа (только у
    топапов, иначе []), чтобы перевести данные покупателя в формат FZ без
    лишнего запроса.
    """
    fz_fields: list[dict[str, Any]] = []
    if position.source_type == SourceType.TOPUP:
        data = fz_client.get_topup_offers(position.fz_category_id)
        offer = next(o for o in data["offers"] if o["offer_id"] == position.fz_offer_id)
        live_price = Decimal(offer["price_usd"])
        fz_fields = data.get("fields", [])
    elif position.source_type == SourceType.GIFTCARD:
        data = fz_client.get_giftcard_offers(position.fz_category_id)
        offer = next(o for o in data["offers"] if o["card_id"] == position.fz_offer_id)
        live_price = Decimal(offer["price_usd"])
    elif position.source_type == SourceType.STEAM_GIFT:
        data = fz_client.get_steam_gift_offers(position.fz_appid)
        offer = next(o for o in data["offers"] if o["sub_id"] == position.fz_sub_id)
        region_price = next(r["price"] for r in offer["regions"] if r["region"] == position.region)
        live_price = Decimal(region_price)
    elif buyer_fields_for(position.source_type):
        # Steam top-up / Telegram: цена единицы (или плана) из тех же эндпоинтов,
        # что и при синхронизации (fz_special.py).
        from app.sync.fz_special import fetch_specs

        spec = next(s for s in fetch_specs(fz_client, SourceType(position.source_type))
                    if s.external_id == position.external_id)
        live_price = spec.price_usd
        fz_fields = buyer_fields_for(position.source_type)
    else:
        raise OrderProcessingError(f"Неизвестный source_type: {position.source_type}")

    check = check_price_deviation(position.last_known_price_usd, live_price, config)
    return check.within_threshold, live_price, fz_fields, check.deviation_percent


def normalize_telegram_username(value: str) -> str:
    """«durov», «@durov», «t.me/durov», «https://t.me/durov» → «@durov»."""
    username = value.strip()
    for prefix in ("https://", "http://", "t.me/", "telegram.me/"):
        if username.lower().startswith(prefix):
            username = username[len(prefix):]
    return "@" + username.strip().lstrip("@").strip("/")


def place_fz_order(
    fz_client: FazerCardsClient,
    position: Position,
    buyer_data: dict[str, str],
    idempotency_key: str,
    topup_fields: Optional[dict[str, str]] = None,
    quantity: int = 1,
) -> dict[str, Any]:
    """Заказывает товар у FazerCards, метод зависит от source_type.
    idempotency_key должен быть детерминированным (используем invoice_id)
    — чтобы повторная попытка после таймаута не задвоила заказ/списание.
    topup_fields — данные покупателя, уже переведённые в ключи/значения FZ
    (map_buyer_data_to_fz_fields); для топапа обязательны.
    """
    if position.source_type == SourceType.TOPUP:
        if topup_fields is None:
            raise OrderProcessingError("Для топапа не переданы поля покупателя в формате FZ")
        return fz_client.order_topup(
            position.fz_category_id,
            position.fz_offer_id,
            topup_fields,
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
    if position.source_type in (SourceType.STEAM_TOPUP, SourceType.TELEGRAM_STARS, SourceType.TELEGRAM_PREMIUM):
        if not topup_fields:
            raise OrderProcessingError("Не переданы данные покупателя (логин Steam / username Telegram)")
        if position.source_type == SourceType.STEAM_TOPUP:
            return fz_client.order_steam_topup(
                topup_fields["steam_login"], position.fz_category_id, quantity,
                idempotency_key=idempotency_key,
            )
        username = normalize_telegram_username(topup_fields["telegram_username"])
        if position.source_type == SourceType.TELEGRAM_STARS:
            return fz_client.buy_telegram_stars(username, quantity, idempotency_key=idempotency_key)
        return fz_client.buy_telegram_premium(username, int(position.fz_offer_id), idempotency_key=idempotency_key)
    raise OrderProcessingError(f"Неизвестный source_type: {position.source_type}")


def check_before_order(
    fz_client: FazerCardsClient, position: Position, topup_fields: Optional[dict[str, str]], quantity: int
) -> Optional[str]:
    """Проверки до списания у FZ (этап 7). Текст причины — заказ в ручной
    разбор, None — можно заказывать."""
    raw = position.raw_payload or {}
    if is_unit_priced(position.source_type):
        low, high = int(raw.get("min_units", 1)), int(raw.get("max_units", quantity))
        if not low <= quantity <= high:
            return f"количество {quantity} вне диапазона {low}–{high}"
    if position.source_type == SourceType.STEAM_TOPUP:
        login = (topup_fields or {}).get("steam_login", "")
        result = fz_client.check_steam_login(login)
        if not result.get("can_refill"):
            return f"логин Steam {login!r} нельзя пополнить (FZ check-login: {result})"
    return None


def is_transient_fz_error(error: Exception) -> bool:
    """Повторять ли попытку: сеть, 5xx, исчерпанный лимит 429. Остальные 4xx
    — бизнес-отказ (нет баланса, неверный ID…), повтор ничего не даст."""
    if isinstance(error, (httpx.TransportError, FazerCardsRateLimitError)):
        return True
    return isinstance(error, FazerCardsError) and error.status_code >= 500


def describe_fz_error(error: Exception) -> str:
    if isinstance(error, FazerCardsError):
        # FZ пишет текст с точкой в конце — в алерте после него идёт «. …»
        return f"FazerCards {error.status_code}: {str(error.error).rstrip('.')}"
    return f"FazerCards недоступен: {type(error).__name__}: {error}"


def fz_order_of(response: dict[str, Any]) -> dict[str, Any]:
    """Ответ FZ на заказ и GET /orders/{id} — {"ok": true, "order": {...}}."""
    return response.get("order", response)


# ----------------------------------------------------------------------
# 7. Выдача
# ----------------------------------------------------------------------


def deliver_order(
    session: Session, ggsell_v1: GGSellV1Client, order: Order, fz_response: dict[str, Any]
) -> Order:
    position = order.listing.position
    message = format_delivery_message(position, order.buyer_data or {}, fz_response, quantity=order.quantity or 1)
    if message is None:
        return _to_manual_review(
            session, order,
            "FZ выполнил заказ, но кодов в ответе нет",
            f"Заказ {order.ggsell_invoice_id}: FZ выполнил заказ {order.fz_order_id}, но коды не найдены "
            f"в ответе — выдайте вручную. Ответ FZ: {fz_order_of(fz_response)}",
        )
    try:
        ggsell_v1.create_message(int(order.ggsell_invoice_id), message)
    except GGSellError as e:
        return _to_manual_review(
            session, order,
            f"Товар получен у FZ, но не отправлен в чат: {e.status_code} {e.payload}",
            f"Заказ {order.ggsell_invoice_id}: товар получен у FZ, но отправка в чат не удалась "
            f"({e.status_code}). Выдать вручную! Сообщение покупателю:\n{message}",
        )
    order.status = OrderStatus.DELIVERED
    order.delivered_message = message
    session.commit()
    logger.info("Заказ %s выдан покупателю", order.ggsell_invoice_id)
    return order


def _apply_fz_result(
    session: Session, ggsell_v1: GGSellV1Client, order: Order, fz_response: dict[str, Any]
) -> Order:
    """Статус заказа FZ → что делаем с нашим заказом."""
    fz_order = fz_order_of(fz_response)
    order.fz_order_id = order.fz_order_id or str(fz_order.get("id") or "") or None
    order.fz_status = fz_order.get("status")
    if order.fz_status == FZ_COMPLETED:
        session.commit()
        return deliver_order(session, ggsell_v1, order, fz_response)
    if order.fz_status in FZ_FINAL_FAILURES:
        return _to_manual_review(
            session, order,
            f"FZ: заказ {order.fz_order_id} в статусе {order.fz_status}",
            f"Заказ {order.ggsell_invoice_id}: FZ не выполнил заказ {order.fz_order_id} "
            f"(статус {order.fz_status}). Требуется ручная обработка / возврат покупателю.",
        )
    order.status = OrderStatus.ORDERED_UPSTREAM
    session.commit()
    logger.info(
        "Заказ %s: FZ %s в статусе %s — ждём", order.ggsell_invoice_id, order.fz_order_id, order.fz_status
    )
    return order


# ----------------------------------------------------------------------
# Точки входа
# ----------------------------------------------------------------------


def process_new_order(
    session: Session,
    fz_client: FazerCardsClient,
    ggsell_v1: GGSellV1Client,
    pricing_config: PricingConfig,
    invoice_id: str,
    expected_offer_id: Optional[int] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Order:
    """Вызывается из вебхука (в фоне) и из страховочной джобы. Идемпотентна:
    повторный вызов для уже взятого в работу заказа ничего не делает."""
    order, ctx = register_order(session, ggsell_v1, invoice_id, expected_offer_id)
    if not claim_order(session, order):
        logger.info("Заказ %s уже в статусе %s — пропускаю", invoice_id, order.status)
        return order

    price_ok, live_price_usd, fz_fields, deviation = verify_price_before_charge(
        fz_client, ctx.position, pricing_config
    )
    order.price_deviation_percent = deviation.quantize(Decimal("0.01"))
    if not price_ok:
        order.status = OrderStatus.PRICE_REJECTED
        order.error_message = (
            f"Цена разошлась: кэш={ctx.position.last_known_price_usd} USD, "
            f"живая={live_price_usd} USD, отклонение {order.price_deviation_percent}%, "
            f"порог={pricing_config.max_price_deviation_percent}%"
        )
        session.commit()
        notify_admin(
            f"Заказ {invoice_id}: цена у FZ разошлась больше порога, отправлен в ручной режим. "
            f"{order.error_message}\n\n{REPLY_HINT}"
        )
        return order

    topup_fields = None
    if ctx.position.source_type == SourceType.TOPUP or buyer_fields_for(ctx.position.source_type):
        try:
            topup_fields = map_buyer_data_to_fz_fields(fz_fields, ctx.buyer_data)
        except BuyerDataError as e:
            return _to_manual_review(
                session, order,
                f"Данные покупателя не подходят для заказа у FZ: {e}",
                f"Заказ {invoice_id}: данные покупателя не подходят для заказа у FZ: {e}. "
                f"Данные покупателя: {ctx.buyer_data}. Требуется ручная обработка.",
            )

    try:
        problem = check_before_order(fz_client, ctx.position, topup_fields, ctx.quantity)
    except (FazerCardsError, httpx.TransportError) as e:
        problem = f"проверка у FZ не удалась — {describe_fz_error(e)}"
    if problem:
        return _to_manual_review(
            session, order, problem,
            f"Заказ {invoice_id}: {problem}. Заказ у FZ НЕ сделан. "
            f"Данные покупателя: {ctx.buyer_data}. Требуется ручная обработка.",
        )

    no_retry = SourceType(ctx.position.source_type) in NO_RETRY_SOURCE_TYPES
    fz_response = None
    last_error: Optional[str] = None
    for attempt in range(1, MAX_FZ_ORDER_RETRIES + 1):
        order.retry_count = attempt
        try:
            fz_response = place_fz_order(
                fz_client, ctx.position, ctx.buyer_data,
                idempotency_key=invoice_id, topup_fields=topup_fields, quantity=ctx.quantity,
            )
            break
        except (FazerCardsError, httpx.TransportError) as e:
            last_error = describe_fz_error(e)
            if is_transient_fz_error(e) and no_retry:
                return _to_manual_review(
                    session, order, last_error,
                    f"Заказ {invoice_id}: сбой связи с FZ при покупке Telegram — {last_error}. "
                    f"Заказ МОГ пройти: проверьте в кабинете FazerCards, прежде чем выдавать вручную "
                    f"или возвращать деньги (повтор мог бы купить второй раз).",
                )
            if not is_transient_fz_error(e):
                return _to_manual_review(
                    session, order, last_error,
                    f"Заказ {invoice_id}: FZ отказал в заказе — {last_error}. "
                    f"Требуется ручная обработка (выдать вручную или вернуть деньги).",
                )
            logger.warning(
                "Попытка %d/%d заказа у FZ для invoice_id=%s не удалась: %s",
                attempt, MAX_FZ_ORDER_RETRIES, invoice_id, last_error,
            )
            if attempt < MAX_FZ_ORDER_RETRIES:
                sleep(FZ_ORDER_RETRY_DELAY_SECONDS)

    if fz_response is None:
        return _to_manual_review(
            session, order, last_error,
            f"Заказ {invoice_id}: FZ недоступен после {MAX_FZ_ORDER_RETRIES} попыток — {last_error}. "
            f"Требуется ручная обработка.",
        )

    order.fz_ordered_at = datetime.now(timezone.utc)
    return _apply_fz_result(session, ggsell_v1, order, fz_response)


def poll_upstream_orders(
    session: Session,
    fz_client: FazerCardsClient,
    ggsell_v1: GGSellV1Client,
    now: Optional[datetime] = None,
    timeout: timedelta = FZ_ORDER_TIMEOUT,
) -> dict[str, int]:
    """Джоба опроса (roadmap 5.2): заказы ORDERED_UPSTREAM → GET /orders/{id}
    у FZ → выдача / ручной разбор / ждём дальше; дольше timeout — ручной разбор."""
    now = now or datetime.now(timezone.utc)
    stats = {"checked": 0, "delivered": 0, "manual": 0, "waiting": 0}
    orders = session.scalars(select(Order).where(Order.status == OrderStatus.ORDERED_UPSTREAM)).all()
    for order in orders:
        stats["checked"] += 1
        try:
            response = fz_client.get_order(order.fz_order_id)
        except (FazerCardsError, httpx.TransportError) as e:
            logger.warning("Опрос FZ по заказу %s не удался: %s", order.ggsell_invoice_id, describe_fz_error(e))
            response = None
        if response is not None:
            _apply_fz_result(session, ggsell_v1, order, response)
        if order.status == OrderStatus.ORDERED_UPSTREAM:
            ordered_at = order.fz_ordered_at
            if ordered_at is not None and ordered_at.tzinfo is None:
                ordered_at = ordered_at.replace(tzinfo=timezone.utc)
            if ordered_at is not None and now - ordered_at > timeout:
                _to_manual_review(
                    session, order,
                    f"FZ не выполнил заказ {order.fz_order_id} за {timeout}",
                    f"Заказ {order.ggsell_invoice_id}: FZ {order.fz_order_id} в статусе {order.fz_status} "
                    f"дольше {int(timeout.total_seconds() // 60)} мин. Проверьте вручную.",
                )
        key = {OrderStatus.DELIVERED: "delivered", OrderStatus.MANUAL_REVIEW: "manual"}.get(order.status, "waiting")
        stats[key] += 1
    return stats
