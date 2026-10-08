"""Тексты и клавиатуры бота администратора — чистые
функции от сессии БД, без обращений к Telegram, чтобы их было легко
тестировать. Отправка и обработка нажатий — app/bot/handlers.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import settings
from app.models.listing import Listing, ListingStatus
from app.models.order import Order, OrderStatus
from app.models.position import Position
from app.pricing.exchange_rate import current_rate
from app.pricing.listing_price import effective_markup, listing_price_rub, load_pricing_config

MSK = timezone(timedelta(hours=3))  # в Москве нет перехода на летнее время

BTN_STATUS = "📊 Статус"
BTN_PRICER = "🤖 Авто-прайсер"
BTN_ORDERS = "🧾 Заказы"
BTN_MANUAL = "⚠️ Ручной разбор"

ORDER_STATUS_NAMES = {
    OrderStatus.PENDING: "⏳ ожидает",
    OrderStatus.PROCESSING: "⚙️ в работе",
    OrderStatus.ORDERED_UPSTREAM: "⚙️ ждём FZ",
    OrderStatus.DELIVERED: "✅ выдан",
    OrderStatus.PRICE_REJECTED: "⚠️ цена разошлась",
    OrderStatus.FAILED: "❌ ошибка",
    OrderStatus.MANUAL_REVIEW: "⚠️ ручной разбор",
}
LISTING_STATUS_NAMES = {
    ListingStatus.ACTIVE: "активен",
    ListingStatus.PAUSED: "на паузе",
    ListingStatus.DRAFT: "черновик",
    ListingStatus.ARCHIVED: "удалён",
}
NEEDS_ATTENTION = (OrderStatus.MANUAL_REVIEW, OrderStatus.PRICE_REJECTED)


def fmt_time(value: Optional[datetime]) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(MSK).strftime("%d.%m %H:%M МСК")


def fmt_num(value: Optional[Decimal]) -> str:
    if value is None:
        return "—"
    return f"{value.normalize():f}"


def button(text: str, data: str) -> dict:
    return {"text": text, "callback_data": data}


def inline(*rows: list[dict]) -> dict:
    return {"inline_keyboard": [row for row in rows if row]}


def main_menu() -> dict:
    return {
        "keyboard": [[{"text": BTN_STATUS}, {"text": BTN_PRICER}], [{"text": BTN_ORDERS}, {"text": BTN_MANUAL}]],
        "resize_keyboard": True,
    }


def help_text() -> str:
    return (
        "Бот администратора FazerCards ↔ GGSell.\n\n"
        f"{BTN_STATUS} — состояние системы\n"
        f"{BTN_PRICER} — курс, наценка, прайсер ВКЛ/ВЫКЛ, обновить цены\n"
        f"{BTN_ORDERS} — последние заказы\n"
        f"{BTN_MANUAL} — заказы, которые надо разобрать вручную\n\n"
        "Команды: /lot <id или название> — найти лот, /pause <id> и /activate <id> — "
        "пауза и публикация лота на GGSell, /sync — обновить цены сейчас, /cancel — отменить ввод.\n\n"
        "Чтобы написать покупателю — ответьте (reply) на алерт о его заказе."
    )


# ----------------------------------------------------------------------
# Статус (6.4)
# ----------------------------------------------------------------------


def _count_by(session: Session, column, *where) -> dict[str, int]:
    rows = session.execute(select(column, func.count()).where(*where).group_by(column)).all()
    return {str(key): count for key, count in rows}


def status_text(session: Session, fz_balance: Optional[dict[str, Any]] = None,
                now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    rate = current_rate(session)
    positions = session.scalar(select(func.count()).select_from(Position))
    missing = session.scalar(select(func.count()).where(Position.fz_missing_since.is_not(None)))
    out_of_stock = session.scalar(select(func.count()).where(Position.fz_out_of_stock_since.is_not(None)))
    listings = _count_by(session, Listing.status)
    orders = _count_by(session, Order.status, Order.created_at >= now - timedelta(hours=24))
    attention = session.scalar(select(func.count()).where(Order.status.in_(NEEDS_ATTENTION)))

    lines = [
        "📊 Статус",
        "",
        f"Прайсер: {'🟢 включён' if settings.pricer_enabled(session) else '🔴 выключен'}",
        f"Курс: 1$ = {fmt_num(rate.rate)} ₽ (ЦБ {fmt_num(rate.cbr_rate)} + {fmt_num(rate.premium_percent)}%)",
        f"Наценка: {fmt_num(settings.global_markup_percent(session))}%",
        f"Цены обновлены: {fmt_time(settings.get_datetime(session, settings.PRICES_UPDATED_AT))}",
        "",
        f"Каталог FZ: {positions} позиций" + (f", пропало у FZ: {missing}" if missing else "")
        + (f", нет в наличии: {out_of_stock}" if out_of_stock else ""),
        "Лоты GGSell: " + (", ".join(
            f"{LISTING_STATUS_NAMES.get(ListingStatus(s), s)} {n}" for s, n in sorted(listings.items())
        ) or "нет"),
        "Заказы за 24 ч: " + (", ".join(
            f"{ORDER_STATUS_NAMES.get(OrderStatus(s), s)} {n}" for s, n in sorted(orders.items())
        ) or "нет"),
        f"Требуют внимания: {attention}" + (" ⚠️" if attention else ""),
    ]
    if fz_balance is not None:
        lines.append(f"Баланс FazerCards: {fz_balance.get('balance')} {fz_balance.get('currency', '')}".rstrip())
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Панель «Авто-прайсер» (6.8)
# ----------------------------------------------------------------------


def pricer_panel(session: Session) -> tuple[str, dict]:
    rate = current_rate(session)
    enabled = settings.pricer_enabled(session)
    requested = bool(settings.get(session, settings.PRICE_SYNC_REQUESTED_AT))
    text = "\n".join([
        "🤖 Авто-Прайсер",
        "",
        f"Статус: {'🟢 ВКЛ' if enabled else '🔴 ВЫКЛ'}",
        f"Курс: 1$ = {fmt_num(rate.rate)} ₽",
        f"   ЦБ {fmt_num(rate.cbr_rate)} ₽ + надбавка {fmt_num(rate.premium_percent)}%, ЦБ от {fmt_time(rate.updated_at)}",
        f"Наценка (глобальная): {fmt_num(settings.global_markup_percent(session))}%",
        f"Последнее обновление цен: {fmt_time(settings.get_datetime(session, settings.PRICES_UPDATED_AT))}",
    ] + (["", "⏳ Обновление цен запрошено, итог придёт сообщением."] if requested else []))
    markup = inline(
        [button("🔴 Выключить прайсер" if enabled else "🟢 Включить прайсер", "pricer:toggle")],
        [button("✏️ Наценка", "pricer:markup"), button("💱 Надбавка к курсу", "pricer:premium")],
        [button("🔎 Наценка лота", "pricer:lot")],
        [button("🔄 Обновить цены СЕЙЧАС", "pricer:sync")],
        [button("↻ Обновить панель", "pricer:show")],
    )
    return text, markup


# ----------------------------------------------------------------------
# Заказы (6.4)
# ----------------------------------------------------------------------


def _order_line(order: Order) -> str:
    name = order.listing.position.name if order.listing else "?"
    return (f"{fmt_time(order.created_at)} · Заказ {order.ggsell_invoice_id} · "
            f"{ORDER_STATUS_NAMES.get(OrderStatus(order.status), order.status)}\n"
            f"   {name} · {fmt_num(order.price_at_sale_rub)} ₽")


def orders_text(session: Session, limit: int = 10) -> str:
    orders = session.scalars(select(Order).order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)).all()
    if not orders:
        return "🧾 Заказов пока нет."
    return f"🧾 Последние заказы ({len(orders)}):\n\n" + "\n".join(_order_line(o) for o in orders)


def attention_orders(session: Session, limit: int = 10) -> list[Order]:
    return session.scalars(
        select(Order).where(Order.status.in_(NEEDS_ATTENTION)).order_by(Order.created_at).limit(limit)
    ).all()


def manual_order_card(order: Order) -> tuple[str, dict]:
    """Одно сообщение на заказ: текст начинается с «Заказ N» — ответ на него
    бот отправит покупателю (как и ответ на алерт)."""
    data = "\n".join(f"   {k}: {v}" for k, v in (order.buyer_data or {}).items())
    text = (
        f"Заказ {order.ggsell_invoice_id} — {ORDER_STATUS_NAMES.get(OrderStatus(order.status), order.status)}\n"
        f"{order.listing.position.name if order.listing else '?'}\n"
        f"Оплачено: {fmt_num(order.price_at_sale_rub)} ₽, создан {fmt_time(order.created_at)}\n"
        + (f"Данные покупателя:\n{data}\n" if data else "")
        + f"Причина: {order.error_message or '—'}\n\n"
        "↩️ Ответьте на это сообщение, чтобы написать покупателю."
    )
    return text, inline([button("✅ Выдан вручную — закрыть", f"done:{order.ggsell_invoice_id}")])


# ----------------------------------------------------------------------
# Лоты: поиск и индивидуальная наценка (3.4b, 6.8)
# ----------------------------------------------------------------------


def find_listings(session: Session, query: str, limit: int = 10) -> list[Listing]:
    query = query.strip()
    stmt = select(Listing).join(Listing.position).where(Listing.status != ListingStatus.ARCHIVED)
    if query.isdigit():
        stmt = stmt.where(or_(Listing.ggsell_offer_id == int(query), Listing.id == int(query)))
    else:
        for word in query.split():
            stmt = stmt.where(Position.name.ilike(f"%{word}%"))
    return session.scalars(stmt.order_by(Position.name).limit(limit)).all()


def lot_search_results(listings: list[Listing], query: str) -> tuple[str, Optional[dict]]:
    if not listings:
        return f"По запросу «{query}» лотов не найдено. Пришлите id лота GGSell или часть названия.", None
    rows = [[button(f"{l.position.name} · {fmt_num(l.price_rub)} ₽"[:60], f"lot:{l.id}")] for l in listings]
    more = "\n(показаны первые 10 — уточните запрос)" if len(listings) == 10 else ""
    return f"Найдено: {len(listings)}. Выберите лот:{more}", inline(*rows)


def lot_card(session: Session, listing: Listing) -> tuple[str, dict]:
    config = load_pricing_config(session)
    markup = effective_markup(config, listing.markup_percent)
    status = LISTING_STATUS_NAMES.get(ListingStatus(listing.status), listing.status)
    text = "\n".join([
        f"🏷 {listing.position.name}",
        f"Лот GGSell {listing.ggsell_offer_id} · {status}",
        "",
        f"Цена на витрине: {fmt_num(listing.price_rub)} ₽",
        f"Расчётная цена сейчас: {fmt_num(listing_price_rub(listing, config))} ₽",
        f"Цена FZ: ${fmt_num(listing.position.last_known_price_usd)}",
        f"Наценка: {fmt_num(markup)}% " + ("(индивидуальная)" if listing.markup_percent is not None else "(глобальная)"),
        f"Комиссия GGSell: {fmt_num((listing.ggsell_fee + listing.ggsell_payment_fee) * 100)}%",
    ])
    rows = [[button("✏️ Своя наценка", f"lotmk:{listing.id}")]]
    if listing.markup_percent is not None:
        rows.append([button("↩️ Вернуть глобальную", f"lotreset:{listing.id}")])
    return text, inline(*rows)
