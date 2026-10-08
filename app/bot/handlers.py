"""Обработка апдейтов Telegram ботом администратора.

  - Доступ — только chat_id из ADMIN_TELEGRAM_CHAT_ID (6.5), остальных бот
    молча игнорирует (пишет в лог).
  - Ответ (reply) на алерт о заказе или карточку из «Ручного разбора» →
    подтверждение → create_message покупателю в чат заказа (6.3). Номер
    заказа берётся из текста сообщения, на которое ответили («Заказ N»).
  - Панель «Авто-прайсер» (6.8) и индивидуальная наценка лота: после
    изменения наценки или надбавки сразу запрашивается синхронизация цен —
    её выполняет scheduler (app/sync/scheduler.py), итог приходит сообщением.

Состояние ввода (ждём число наценки, поисковый запрос…) — в памяти процесса:
админ один-два, при рестарте бота достаточно начать ввод заново.
"""

from __future__ import annotations

import logging
import re
import secrets
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import settings
from app.bot import views
from app.clients.telegram import TelegramClient
from app.models.listing import Listing
from app.models.order import Order, OrderStatus
from app.pricing.exchange_rate import set_premium
from app.pricing.listing_price import set_listing_markup

logger = logging.getLogger(__name__)

ORDER_REF = re.compile(r"Заказ (\d+)")


class Services(Protocol):
    """Внешние системы, которые нужны боту (в тестах — подделки)."""

    def fz_balance(self) -> Optional[dict[str, Any]]: ...

    def send_to_buyer(self, invoice_id: int, text: str) -> None: ...

    def set_offer_active(self, offer_id: int, active: bool) -> Any: ...


def parse_percent(text: str) -> Decimal:
    try:
        return Decimal(text.strip().replace("%", "").replace(",", ".").strip())
    except InvalidOperation:
        raise ValueError(f"«{text}» — не число. Пришлите число, например 15 или 12.5")


@dataclass
class BotApp:
    tg: TelegramClient
    services: Services
    session_factory: Callable[[], Session]
    admin_ids: list[int]
    state: dict[int, tuple] = field(default_factory=dict)  # chat_id → чего ждём
    outbox: dict[str, tuple[int, str]] = field(default_factory=dict)  # token → (заказ, текст)

    # ------------------------------------------------------------------
    # Вход
    # ------------------------------------------------------------------

    def handle_update(self, update: dict) -> None:
        if "callback_query" in update:
            query = update["callback_query"]
            chat_id = query.get("message", {}).get("chat", {}).get("id")
            if not self._allowed(chat_id, query.get("from")):
                return
            self._run(chat_id, self._on_callback, chat_id, query)
        elif "message" in update:
            message = update["message"]
            chat_id = message.get("chat", {}).get("id")
            if not self._allowed(chat_id, message.get("from")) or "text" not in message:
                return
            self._run(chat_id, self._on_message, chat_id, message)

    def _allowed(self, chat_id: Optional[int], user: Optional[dict]) -> bool:
        if chat_id in self.admin_ids:
            return True
        logger.warning("Сообщение боту не от админа: chat_id=%s, user=%s", chat_id, (user or {}).get("username"))
        return False

    def _run(self, chat_id: int, handler: Callable, *args: Any) -> None:
        session = self.session_factory()
        try:
            handler(session, *args)
        except Exception as e:  # noqa: BLE001 — бот не должен падать из-за одной команды
            session.rollback()
            logger.exception("Ошибка обработки апдейта")
            self.tg.send_message(chat_id, f"❗ Ошибка: {e}")
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Сообщения
    # ------------------------------------------------------------------

    def _on_message(self, session: Session, chat_id: int, message: dict) -> None:
        text = message["text"].strip()

        if text.startswith("/"):
            self.state.pop(chat_id, None)
            command, _, arg = text.partition(" ")
            return self._on_command(session, chat_id, command.split("@")[0].lower(), arg.strip())

        menu = {
            views.BTN_STATUS: self._show_status,
            views.BTN_PRICER: self._show_pricer,
            views.BTN_ORDERS: self._show_orders,
            views.BTN_MANUAL: self._show_manual,
        }
        if text in menu:
            self.state.pop(chat_id, None)
            return menu[text](session, chat_id)

        replied = message.get("reply_to_message") or {}
        match = ORDER_REF.search(replied.get("text", "")) if replied.get("from", {}).get("is_bot") else None
        if match:
            return self._confirm_buyer_message(chat_id, int(match.group(1)), text)

        if chat_id in self.state:
            return self._on_input(session, chat_id, text)

        self.tg.send_message(chat_id, views.help_text(), reply_markup=views.main_menu())

    def _on_command(self, session: Session, chat_id: int, command: str, arg: str) -> None:
        if command in ("/start", "/menu", "/help"):
            self.tg.send_message(chat_id, views.help_text(), reply_markup=views.main_menu())
        elif command == "/status":
            self._show_status(session, chat_id)
        elif command == "/pricer":
            self._show_pricer(session, chat_id)
        elif command == "/orders":
            self._show_orders(session, chat_id)
        elif command == "/manual":
            self._show_manual(session, chat_id)
        elif command == "/sync":
            self._request_sync(session)
            self.tg.send_message(chat_id, "🔄 Обновление цен запущено — итог придёт сообщением (обычно 2–3 мин).")
        elif command == "/cancel":
            self.tg.send_message(chat_id, "Ок, отменено.", reply_markup=views.main_menu())
        elif command == "/lot":
            if arg:
                self._search_lots(session, chat_id, arg)
            else:
                self.state[chat_id] = ("lot_search",)
                self.tg.send_message(chat_id, "Пришлите id лота GGSell или часть названия.")
        elif command in ("/pause", "/activate"):
            self._set_offer_active(session, chat_id, arg, active=command == "/activate")
        else:
            self.tg.send_message(chat_id, "Не знаю такой команды.\n\n" + views.help_text())

    def _on_input(self, session: Session, chat_id: int, text: str) -> None:
        kind, *args = self.state[chat_id]
        if kind == "lot_search":
            self.state.pop(chat_id)
            return self._search_lots(session, chat_id, text)
        try:
            value = parse_percent(text)
            if kind == "markup":
                settings.set_global_markup_percent(session, value)
                done = f"✅ Глобальная наценка: {views.fmt_num(value)}%."
            elif kind == "premium":
                info = set_premium(session, value)
                done = (f"✅ Надбавка к курсу ЦБ: {views.fmt_num(value)}%, "
                        f"курс теперь 1$ = {views.fmt_num(info.rate)} ₽.")
            elif kind == "lot_markup":
                listing = session.get(Listing, args[0])
                set_listing_markup(listing, value)
                done = f"✅ Наценка лота «{listing.position.name}»: {views.fmt_num(value)}%."
            else:
                raise RuntimeError(f"Неизвестное состояние ввода: {kind}")
        except ValueError as e:
            self.tg.send_message(chat_id, f"{e}\n/cancel — отменить.")
            return
        self.state.pop(chat_id)
        self._request_sync(session)
        self.tg.send_message(chat_id, done + "\nЦены на витрине пересчитываются — итог придёт сообщением.")
        if kind == "lot_markup":
            self._send_lot(session, chat_id, args[0])
        else:
            self._show_pricer(session, chat_id)

    # ------------------------------------------------------------------
    # Кнопки
    # ------------------------------------------------------------------

    def _on_callback(self, session: Session, chat_id: int, query: dict) -> None:
        data = query.get("data", "")
        message_id = query["message"]["message_id"]
        action, _, arg = data.partition(":")
        notice: Optional[str] = None

        if action == "pricer":
            if arg == "toggle":
                enabled = not settings.pricer_enabled(session)
                settings.set_pricer_enabled(session, enabled)
                session.commit()
                notice = "Прайсер включён" if enabled else "Прайсер выключен — цены на GGSell не меняются"
            elif arg == "sync":
                self._request_sync(session)
                notice = "Обновление цен запущено"
            elif arg in ("markup", "premium"):
                self.state[chat_id] = (arg,)
                current = (settings.global_markup_percent(session) if arg == "markup"
                           else settings.get_decimal(session, settings.RATE_PREMIUM_PERCENT))
                what = "глобальную наценку" if arg == "markup" else "надбавку к курсу ЦБ"
                self.tg.send_message(chat_id, f"Пришлите {what} в % (сейчас {views.fmt_num(current)}). /cancel — отмена.")
            elif arg == "lot":
                self.state[chat_id] = ("lot_search",)
                self.tg.send_message(chat_id, "Пришлите id лота GGSell или часть названия.")
            if arg in ("toggle", "sync", "show"):
                text, markup = views.pricer_panel(session)
                self._edit(chat_id, message_id, text, markup)
        elif action == "lot":
            self._send_lot(session, chat_id, int(arg))
        elif action == "lotmk":
            listing = session.get(Listing, int(arg))
            self.state[chat_id] = ("lot_markup", listing.id)
            self.tg.send_message(chat_id, f"Пришлите наценку в % для «{listing.position.name}». /cancel — отмена.")
        elif action == "lotreset":
            listing = session.get(Listing, int(arg))
            set_listing_markup(listing, None)
            self._request_sync(session)
            notice = "Лот снова на глобальной наценке, цены пересчитываются"
            self._send_lot(session, chat_id, listing.id)
        elif action == "send":
            self._send_buyer_message(session, chat_id, message_id, arg)
        elif action == "cancel":
            self.outbox.pop(arg, None)
            self._edit(chat_id, message_id, "Отменено — покупателю ничего не отправлено.")
        elif action == "done":
            notice = self._close_order(session, chat_id, message_id, arg, query["message"].get("text", ""))
        self.tg.answer_callback_query(query["id"], notice)

    # ------------------------------------------------------------------
    # Экраны
    # ------------------------------------------------------------------

    def _show_status(self, session: Session, chat_id: int) -> None:
        try:
            balance = self.services.fz_balance()
        except Exception:  # noqa: BLE001 — статус показываем и без баланса
            logger.exception("Баланс FZ не получен")
            balance = None
        self.tg.send_message(chat_id, views.status_text(session, balance))

    def _show_pricer(self, session: Session, chat_id: int) -> None:
        text, markup = views.pricer_panel(session)
        self.tg.send_message(chat_id, text, reply_markup=markup)

    def _show_orders(self, session: Session, chat_id: int) -> None:
        self.tg.send_message(chat_id, views.orders_text(session))

    def _show_manual(self, session: Session, chat_id: int) -> None:
        orders = views.attention_orders(session)
        if not orders:
            self.tg.send_message(chat_id, "✅ Заказов на ручном разборе нет.")
            return
        self.tg.send_message(chat_id, f"⚠️ Требуют внимания: {len(orders)}")
        for order in orders:
            text, markup = views.manual_order_card(order)
            self.tg.send_message(chat_id, text, reply_markup=markup)

    def _search_lots(self, session: Session, chat_id: int, query: str) -> None:
        listings = views.find_listings(session, query)
        if len(listings) == 1:
            return self._send_lot(session, chat_id, listings[0].id)
        text, markup = views.lot_search_results(listings, query)
        self.tg.send_message(chat_id, text, reply_markup=markup)

    def _send_lot(self, session: Session, chat_id: int, listing_id: int) -> None:
        text, markup = views.lot_card(session, session.get(Listing, listing_id))
        self.tg.send_message(chat_id, text, reply_markup=markup)

    # ------------------------------------------------------------------
    # Действия
    # ------------------------------------------------------------------

    def _request_sync(self, session: Session) -> None:
        settings.request_price_sync(session)
        session.commit()

    def _set_offer_active(self, session: Session, chat_id: int, arg: str, active: bool) -> None:
        listing = session.scalar(select(Listing).where(Listing.ggsell_offer_id == int(arg))) if arg.isdigit() else None
        if listing is None:
            self.tg.send_message(chat_id, "Пришлите id лота GGSell из наших лотов, например /pause 103310519")
            return
        self.services.set_offer_active(listing.ggsell_offer_id, active)
        what = "публикацию" if active else "паузу"
        self.tg.send_message(chat_id, f"✅ Запрос на {what} лота {listing.ggsell_offer_id} отправлен в GGSell. "
                                      "Статус в боте обновится со следующей синхронизацией.")

    def _confirm_buyer_message(self, chat_id: int, invoice_id: int, text: str) -> None:
        token = secrets.token_hex(4)
        self.outbox[token] = (invoice_id, text)
        self.tg.send_message(
            chat_id,
            f"Отправить покупателю в чат заказа {invoice_id}?\n\n{text}",
            reply_markup=views.inline([views.button("📨 Отправить", f"send:{token}"),
                                       views.button("Отмена", f"cancel:{token}")]),
        )

    def _send_buyer_message(self, session: Session, chat_id: int, message_id: int, token: str) -> None:
        item = self.outbox.pop(token, None)
        if item is None:
            self._edit(chat_id, message_id, "Это сообщение уже отправлено или устарело (бот перезапускался) — "
                                            "ответьте на алерт ещё раз.")
            return
        invoice_id, text = item
        self.services.send_to_buyer(invoice_id, text)
        logger.info("Сообщение покупателю заказа %s отправлено из бота", invoice_id)
        order = session.scalar(select(Order).where(Order.ggsell_invoice_id == str(invoice_id)))
        markup = None
        if order is not None and order.status in views.NEEDS_ATTENTION:
            markup = views.inline([views.button("✅ Выдан вручную — закрыть", f"done:{invoice_id}")])
        self._edit(chat_id, message_id, f"✅ Отправлено покупателю заказа {invoice_id}:\n\n{text}", markup)

    def _close_order(self, session: Session, chat_id: int, message_id: int, invoice_id: str, old_text: str) -> str:
        order = session.scalar(select(Order).where(Order.ggsell_invoice_id == invoice_id))
        if order is None:
            return "Заказ не найден"
        if order.status not in views.NEEDS_ATTENTION:
            return f"Заказ уже в статусе {order.status}"
        order.status = OrderStatus.DELIVERED
        order.delivered_message = (order.delivered_message or "") + "\n[выдан вручную, закрыт из бота]"
        session.commit()
        self._edit(chat_id, message_id, f"{old_text}\n\n✅ Закрыт: выдан вручную.")
        return "Заказ закрыт"

    def _edit(self, chat_id: int, message_id: int, text: str, markup: Optional[dict] = None) -> None:
        try:
            self.tg.edit_message_text(chat_id, message_id, text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001 — «message is not modified» и т.п.
            if "not modified" not in str(e):
                raise
