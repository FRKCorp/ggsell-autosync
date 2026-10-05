from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import settings
from app.bot import views
from app.bot.handlers import BotApp
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.order import Order, OrderStatus
from app.models.position import Position, SourceType
from app.orders import notifications
from app.sync.scheduler import check_price_sync_request

ADMIN = 111
STRANGER = 999


@pytest.fixture
def factory(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    make = sessionmaker(engine, expire_on_commit=False)
    with make() as s:
        settings.set_value(s, settings.USD_RUB_CBR, Decimal("87.4077"))
        settings.set_value(s, settings.USD_RUB_RATE, Decimal("91.78"))
        settings.set_value(s, settings.RATE_PREMIUM_PERCENT, Decimal("5"))
        settings.set_value(s, settings.GLOBAL_MARKUP_PERCENT, Decimal("15"))
        settings.set_value(s, settings.PRICER_ENABLED, "true")
        position = Position(external_id="topup:mlbb:5", source_type=SourceType.TOPUP, name="Mobile Legends (Global) — 5 Diamonds",
                            last_known_price_usd=Decimal("0.09"), raw_payload={})
        listing = Listing(position=position, ggsell_offer_id=103310519, status=ListingStatus.PAUSED,
                          price_rub=Decimal("12"), price_source_usd_at_sync=Decimal("0.09"),
                          ggsell_fee=Decimal("0.04"), ggsell_payment_fee=Decimal("0.027"))
        s.add_all([position, listing])
        s.flush()
        s.add_all([
            Order(ggsell_invoice_id="53081129", listing=listing, status=OrderStatus.MANUAL_REVIEW,
                  buyer_data={"Player ID": "12345678"}, price_at_sale_rub=Decimal("10"),
                  error_message="FZ отказал: Insufficient balance"),
            Order(ggsell_invoice_id="53000001", listing=listing, status=OrderStatus.DELIVERED,
                  buyer_data={}, price_at_sale_rub=Decimal("12")),
        ])
        s.commit()
    return make


@pytest.fixture
def bot(factory):
    tg = MagicMock()
    tg.send_message.side_effect = lambda chat_id, text, **kw: {"message_id": 1}
    services = MagicMock()
    services.fz_balance.return_value = {"ok": True, "balance": "1.5000", "currency": "USD"}
    return BotApp(tg=tg, services=services, session_factory=factory, admin_ids=[ADMIN])


def msg(text, chat_id=ADMIN, reply_to=None):
    message = {"message_id": 10, "chat": {"id": chat_id}, "from": {"id": chat_id}, "text": text}
    if reply_to is not None:
        message["reply_to_message"] = {"message_id": 5, "from": {"is_bot": True}, "text": reply_to}
    return {"update_id": 1, "message": message}


def click(data, chat_id=ADMIN, text="..."):
    return {"update_id": 2, "callback_query": {"id": "cb", "data": data, "from": {"id": chat_id},
                                               "message": {"message_id": 7, "chat": {"id": chat_id}, "text": text}}}


def sent(bot):
    return [c.args[1] for c in bot.tg.send_message.call_args_list]


def edited(bot):
    return [c.args[2] for c in bot.tg.edit_message_text.call_args_list]


def last_markup(bot):
    return bot.tg.send_message.call_args.kwargs.get("reply_markup")


def callbacks(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row]


# ----------------------------------------------------------------------
# 6.5 доступ
# ----------------------------------------------------------------------


def test_stranger_ignored(bot):
    bot.handle_update(msg("/status", chat_id=STRANGER))
    bot.handle_update(click("pricer:toggle", chat_id=STRANGER))

    bot.tg.send_message.assert_not_called()
    bot.tg.answer_callback_query.assert_not_called()


def test_start_shows_menu(bot):
    bot.handle_update(msg("/start"))

    assert "Бот администратора" in sent(bot)[0]
    assert last_markup(bot)["keyboard"][0][0]["text"] == views.BTN_STATUS


# ----------------------------------------------------------------------
# 6.4 команды
# ----------------------------------------------------------------------


def test_status(bot):
    bot.handle_update(msg(views.BTN_STATUS))

    text = sent(bot)[0]
    assert "Прайсер: 🟢 включён" in text
    assert "1$ = 91.78 ₽ (ЦБ 87.4077 + 5%)" in text
    assert "Наценка: 15%" in text
    assert "на паузе 1" in text
    assert "Требуют внимания: 1" in text
    assert "Баланс FazerCards: 1.5000 USD" in text


def test_status_without_fz_balance(bot):
    bot.services.fz_balance.side_effect = RuntimeError("403 subscription_inactive")

    bot.handle_update(msg("/status"))

    assert "Прайсер" in sent(bot)[0] and "Баланс" not in sent(bot)[0]


def test_orders_list(bot):
    bot.handle_update(msg(views.BTN_ORDERS))

    text = sent(bot)[0]
    assert "Заказ 53081129" in text and "Заказ 53000001" in text and "Mobile Legends" in text


def test_manual_review_cards(bot):
    bot.handle_update(msg(views.BTN_MANUAL))

    texts = sent(bot)
    assert "Требуют внимания: 1" in texts[0]
    assert texts[1].startswith("Заказ 53081129") and "Player ID: 12345678" in texts[1]
    assert "Insufficient balance" in texts[1]
    assert callbacks(last_markup(bot)) == ["done:53081129"]


def test_pause_and_activate_only_our_lots(bot):
    bot.handle_update(msg("/pause 103310519"))
    bot.handle_update(msg("/activate 103310519"))
    bot.handle_update(msg("/pause 42"))

    assert [c.args for c in bot.services.set_offer_active.call_args_list] == [(103310519, False), (103310519, True)]
    assert "из наших лотов" in sent(bot)[-1]


def test_sync_command_requests_sync(bot, factory):
    bot.handle_update(msg("/sync"))

    with factory() as s:
        assert settings.get(s, settings.PRICE_SYNC_REQUESTED_AT)


# ----------------------------------------------------------------------
# 6.8 панель «Авто-прайсер»
# ----------------------------------------------------------------------


def test_pricer_panel(bot):
    bot.handle_update(msg(views.BTN_PRICER))

    text = sent(bot)[0]
    assert "Статус: 🟢 ВКЛ" in text and "Наценка (глобальная): 15%" in text
    assert callbacks(last_markup(bot)) == ["pricer:toggle", "pricer:markup", "pricer:premium", "pricer:lot",
                                           "pricer:sync", "pricer:show"]


def test_toggle_pricer(bot, factory):
    bot.handle_update(click("pricer:toggle"))

    with factory() as s:
        assert settings.pricer_enabled(s) is False
    assert "Статус: 🔴 ВЫКЛ" in edited(bot)[0]

    bot.handle_update(click("pricer:toggle"))
    with factory() as s:
        assert settings.pricer_enabled(s) is True


def test_change_global_markup_requests_sync(bot, factory):
    bot.handle_update(click("pricer:markup"))
    bot.handle_update(msg("abc"))  # не число — просим ещё раз
    bot.handle_update(msg("20,5%"))

    texts = sent(bot)
    assert "сейчас 15" in texts[0]
    assert "не число" in texts[1]
    assert "Глобальная наценка: 20.5%" in texts[2]
    with factory() as s:
        assert settings.global_markup_percent(s) == Decimal("20.5")
        assert settings.get(s, settings.PRICE_SYNC_REQUESTED_AT)
    assert "Наценка (глобальная): 20.5%" in texts[3]  # панель заново


def test_markup_out_of_range_rejected(bot, factory):
    bot.handle_update(click("pricer:markup"))
    bot.handle_update(msg("5000"))

    assert "от 0 до 1000%" in sent(bot)[-1]
    with factory() as s:
        assert settings.global_markup_percent(s) == Decimal("15")


def test_change_premium_recalculates_rate(bot, factory):
    bot.handle_update(click("pricer:premium"))
    bot.handle_update(msg("10"))

    assert "курс теперь 1$ = 96.1485 ₽" in sent(bot)[1]
    with factory() as s:
        assert settings.get_decimal(s, settings.RATE_PREMIUM_PERCENT) == Decimal("10")


def test_sync_button(bot, factory):
    bot.handle_update(click("pricer:sync"))

    with factory() as s:
        assert settings.get(s, settings.PRICE_SYNC_REQUESTED_AT)
    assert "Обновление цен запрошено" in edited(bot)[0]
    bot.tg.answer_callback_query.assert_called_with("cb", "Обновление цен запущено")


def test_cancel_input(bot, factory):
    bot.handle_update(click("pricer:markup"))
    bot.handle_update(msg("/cancel"))
    bot.handle_update(msg("30"))  # уже не ввод наценки

    with factory() as s:
        assert settings.global_markup_percent(s) == Decimal("15")
    assert "Бот администратора" in sent(bot)[-1]


# ----------------------------------------------------------------------
# Индивидуальная наценка лота (3.4b, 6.8)
# ----------------------------------------------------------------------


def test_lot_search_and_individual_markup(bot, factory):
    bot.handle_update(click("pricer:lot"))
    bot.handle_update(msg("mobile diamonds"))  # один найден — сразу карточка

    card = sent(bot)[-1]
    assert "Лот GGSell 103310519" in card and "Наценка: 15% (глобальная)" in card
    assert callbacks(last_markup(bot)) == ["lotmk:1"]

    bot.handle_update(click("lotmk:1"))
    bot.handle_update(msg("30"))

    with factory() as s:
        assert s.get(Listing, 1).markup_percent == Decimal("30")
        assert settings.get(s, settings.PRICE_SYNC_REQUESTED_AT)
    assert "Наценка: 30% (индивидуальная)" in sent(bot)[-1]
    assert callbacks(last_markup(bot)) == ["lotmk:1", "lotreset:1"]

    bot.handle_update(click("lotreset:1"))
    with factory() as s:
        assert s.get(Listing, 1).markup_percent is None


def test_lot_search_by_offer_id_and_not_found(bot):
    bot.handle_update(msg("/lot 103310519"))
    bot.handle_update(msg("/lot netflix"))

    assert "Лот GGSell 103310519" in sent(bot)[0]
    assert "не найдено" in sent(bot)[1]


# ----------------------------------------------------------------------
# 6.3 ответ покупателю
# ----------------------------------------------------------------------


def test_reply_to_alert_sends_to_buyer_after_confirmation(bot, factory):
    alert = f"Заказ 53081129: FZ отказал — Insufficient balance.\n\n{notifications.REPLY_HINT}"

    bot.handle_update(msg("Ваш код: ABCD-1234", reply_to=alert))

    assert "Отправить покупателю в чат заказа 53081129?" in sent(bot)[0]
    bot.services.send_to_buyer.assert_not_called()  # только после подтверждения
    token = callbacks(last_markup(bot))[0].split(":")[1]

    bot.handle_update(click(f"send:{token}"))

    bot.services.send_to_buyer.assert_called_once_with(53081129, "Ваш код: ABCD-1234")
    assert "✅ Отправлено покупателю заказа 53081129" in edited(bot)[0]
    assert callbacks(bot.tg.edit_message_text.call_args.kwargs["reply_markup"]) == ["done:53081129"]

    bot.handle_update(click(f"send:{token}"))  # повторное нажатие — второй раз не шлём
    assert bot.services.send_to_buyer.call_count == 1


def test_reply_cancelled(bot):
    bot.handle_update(msg("текст", reply_to="Заказ 53081129: ..."))
    token = callbacks(last_markup(bot))[1].split(":")[1]

    bot.handle_update(click(f"cancel:{token}"))

    bot.services.send_to_buyer.assert_not_called()
    assert "ничего не отправлено" in edited(bot)[0]


def test_reply_to_non_order_message_is_not_sent(bot):
    bot.handle_update(msg("привет", reply_to="📊 Статус"))

    bot.services.send_to_buyer.assert_not_called()
    assert "Бот администратора" in sent(bot)[0]


def test_close_order_manually(bot, factory):
    bot.handle_update(click("done:53081129", text="Заказ 53081129 — ручной разбор"))

    with factory() as s:
        order = s.scalar(select(Order).where(Order.ggsell_invoice_id == "53081129"))
        assert order.status == OrderStatus.DELIVERED
    assert "Закрыт: выдан вручную" in edited(bot)[0]

    bot.handle_update(click("done:53081129"))  # второй раз — уже закрыт
    bot.tg.answer_callback_query.assert_called_with("cb", "Заказ уже в статусе delivered")


def test_handler_error_reported_not_raised(bot):
    bot.services.set_offer_active.side_effect = RuntimeError("GGSell 500")

    bot.handle_update(msg("/pause 103310519"))

    assert "Ошибка: GGSell 500" in sent(bot)[-1]


# ----------------------------------------------------------------------
# 6.2 алерты и запрос синхронизации
# ----------------------------------------------------------------------


def test_notify_admin_sends_to_all_admins(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "111, 222")
    client = MagicMock()

    notifications.notify_admin("Заказ 1: тест", client=client)

    assert [c.args for c in client.send_message.call_args_list] == [(111, "Заказ 1: тест"), (222, "Заказ 1: тест")]


def test_notify_admin_survives_telegram_failure(monkeypatch, caplog):
    monkeypatch.setenv("ADMIN_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "111")
    client = MagicMock()
    client.send_message.side_effect = RuntimeError("network down")

    notifications.notify_admin("Заказ 1: тест", client=client, sleep=lambda s: None)  # не падает

    assert client.send_message.call_count == notifications.SEND_ATTEMPTS
    assert "ADMIN ALERT: Заказ 1: тест" in caplog.text
    assert "не отправлен после 3 попыток" in caplog.text


def test_notify_admin_retries_until_proxy_recovers(monkeypatch):
    monkeypatch.setenv("ADMIN_TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "111")
    client = MagicMock()
    client.send_message.side_effect = [RuntimeError("Connection refused"), {"message_id": 1}]
    pauses = []

    notifications.notify_admin("Заказ 1: тест", client=client, sleep=pauses.append)

    assert client.send_message.call_count == 2 and pauses == [3]


def test_notify_admin_without_token_only_logs(monkeypatch):
    sent_clients = []
    monkeypatch.setattr(notifications, "TelegramClient", lambda *a, **k: sent_clients.append(1))

    notifications.notify_admin("тест")

    assert sent_clients == []


def test_scheduler_picks_up_sync_request(factory, monkeypatch):
    import app.sync.scheduler as scheduler_module
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    scheduler = MagicMock()

    assert check_price_sync_request(scheduler) is False
    with factory() as s:
        settings.request_price_sync(s)
        s.commit()

    assert check_price_sync_request(scheduler) is True
    assert scheduler.modify_job.call_args.args == ("refresh_prices",)
    assert check_price_sync_request(scheduler) is False  # запрос снят
    with factory() as s:
        assert settings.take_price_sync_report(s) is True  # итог будет отправлен один раз
        assert settings.take_price_sync_report(s) is False


def test_sync_request_waits_while_sync_is_running(factory, monkeypatch):
    """Запрос во время синхронизации не теряется: APScheduler пропустил бы
    запуск (max_instances=1), поэтому запрос ждёт её окончания."""
    import app.sync.scheduler as scheduler_module
    from app.sync import jobs
    monkeypatch.setattr(scheduler_module, "SessionLocal", factory)
    scheduler = MagicMock()
    with factory() as s:
        settings.request_price_sync(s)
        s.commit()

    jobs._running.set()
    try:
        assert check_price_sync_request(scheduler) is False
        scheduler.modify_job.assert_not_called()
    finally:
        jobs._running.clear()

    assert check_price_sync_request(scheduler) is True  # синхронизация закончилась — запускаем
    scheduler.modify_job.assert_called_once()
