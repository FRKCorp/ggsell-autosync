from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsError
from app.clients.ggsell import GGSellError
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.order import Order, OrderStatus
from app.models.position import Position, SourceType
from app.orders.order_processor import (
    OrderProcessingError,
    poll_upstream_orders,
    process_new_order,
)
from app.pricing.calculator import PricingConfig

INVOICE = "53081129"
OFFER_ID = 103153770
CARD_OFFER_ID = 103153771
FIELDS = [{"key": "user_id", "label": "Unique ID", "type": "text"}]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def config():
    return PricingConfig(
        exchange_rate_usd_to_rub=Decimal("95.0"),
        markup_percent=Decimal("15.0"),
        min_margin_percent=Decimal("3.0"),
        max_price_deviation_percent=Decimal("5.0"),
    )


@pytest.fixture
def listings(session):
    topup = Position(
        external_id="topup:8_ball_pool:golden_spin", source_type=SourceType.TOPUP,
        fz_category_id="8_ball_pool", fz_offer_id="golden_spin", region="Global",
        name="8 Ball Pool (Global) — Golden Spin", last_known_price_usd=Decimal("0.7450"),
        raw_payload={"name": "Golden Spin"},
    )
    card = Position(
        external_id="giftcard:google_play_us:25_usd", source_type=SourceType.GIFTCARD,
        fz_category_id="google_play_us", fz_offer_id="25_usd", region="US",
        name="Google Play (US) — 25 USD", last_known_price_usd=Decimal("24.58"),
        raw_payload={"name": "25 USD"},
    )
    session.add_all([topup, card])
    session.flush()
    session.add_all([
        Listing(position_id=topup.id, ggsell_offer_id=OFFER_ID, status=ListingStatus.ACTIVE,
                price_rub=Decimal("82"), price_source_usd_at_sync=Decimal("0.7450")),
        Listing(position_id=card.id, ggsell_offer_id=CARD_OFFER_ID, status=ListingStatus.ACTIVE,
                price_rub=Decimal("2659"), price_source_usd_at_sync=Decimal("24.58")),
    ])
    session.commit()


def order_info(item_id=OFFER_ID, user_data="player123", amount=82.0, profit=78.15, paid=True):
    """get_order_info в формате, снятом с живых заказов (22.09, 01.10)."""
    return {"content": {
        "item_id": item_id,
        "amount": amount,
        "profit": profit,
        "date_pay": "2026-10-01T17:47:37+03:00" if paid else None,
        "options": [{"id": 1, "name": "Unique ID", "user_data": user_data, "user_data_id": 1}],
    }}


def topup_offers(price="0.7450"):
    return {"name": "8 Ball Pool", "fields": FIELDS,
            "offers": [{"offer_id": "golden_spin", "name": "Golden Spin", "price_usd": price}]}


def card_offers(price="24.58"):
    return {"name": "Google Play (US)", "offers": [{"card_id": "25_usd", "name": "25 USD", "price_usd": price}]}


def fz_order(status="completed", order_id="ord-9001", **extra):
    """Ответ FZ на заказ и GET /orders/{id} (docs FZ: {"ok": true, "order": {...}})."""
    return {"ok": True, "order": {"id": order_id, "kind": "topup", "status": status, **extra}}


def clients(item_id=OFFER_ID, **info_kwargs):
    fz = MagicMock()
    fz.get_topup_offers.return_value = topup_offers()
    fz.get_giftcard_offers.return_value = card_offers()
    v1 = MagicMock()
    v1.get_order_info.return_value = order_info(item_id=item_id, **info_kwargs)
    return fz, v1


def run(session, fz, v1, config, **kwargs):
    with patch("app.orders.order_processor.notify_admin") as alert:
        order = process_new_order(session, fz, v1, config, INVOICE, sleep=lambda s: None, **kwargs)
    return order, alert


# ----------------------------------------------------------------------
# Успешные сценарии
# ----------------------------------------------------------------------


def test_topup_completed_immediately_is_delivered(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("completed")

    order, alert = run(session, fz, v1, config, expected_offer_id=OFFER_ID)

    assert order.status == OrderStatus.DELIVERED
    fz.order_topup.assert_called_once_with(
        "8_ball_pool", "golden_spin", {"user_id": "player123"}, idempotency_key=INVOICE
    )
    assert order.fz_order_id == "ord-9001" and order.fz_status == "completed"
    # id_i у create_message — номер заказа (подтверждено поддержкой GGSell)
    v1.create_message.assert_called_once_with(int(INVOICE), order.delivered_message)
    assert "Заказ выполнен" in order.delivered_message
    assert "Unique ID: player123" in order.delivered_message
    assert "{" not in order.delivered_message  # не сырой ответ FZ
    # 5.8: реальная оплата и выплата из заказа, отклонение цены записано
    assert order.price_at_sale_rub == Decimal("82")
    assert order.seller_payout_rub == Decimal("78.15")
    assert order.price_deviation_percent == Decimal("0.00")
    alert.assert_not_called()


def test_processing_then_poll_delivers(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("processing")

    order, _ = run(session, fz, v1, config)

    assert order.status == OrderStatus.ORDERED_UPSTREAM
    assert order.fz_ordered_at is not None
    v1.create_message.assert_not_called()

    fz.get_order.return_value = fz_order("completed")
    stats = poll_upstream_orders(session, fz, v1)

    fz.get_order.assert_called_once_with("ord-9001")
    assert stats == {"checked": 1, "delivered": 1, "manual": 0, "waiting": 0}
    session.refresh(order)
    assert order.status == OrderStatus.DELIVERED
    v1.create_message.assert_called_once()


@pytest.mark.parametrize("fz_status", ["failed", "refund"])
def test_poll_failed_goes_manual_review(session, config, listings, fz_status):
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("processing")
    order, _ = run(session, fz, v1, config)

    fz.get_order.return_value = fz_order(fz_status)
    with patch("app.orders.order_processor.notify_admin") as alert:
        poll_upstream_orders(session, fz, v1)

    assert order.status == OrderStatus.MANUAL_REVIEW
    assert fz_status in order.error_message
    alert.assert_called_once()
    v1.create_message.assert_not_called()


def test_poll_timeout_goes_manual_review(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("processing")
    order, _ = run(session, fz, v1, config)

    fz.get_order.return_value = fz_order("processing")
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    with patch("app.orders.order_processor.notify_admin") as alert:
        stats = poll_upstream_orders(session, fz, v1, now=later)

    assert order.status == OrderStatus.MANUAL_REVIEW
    assert stats["manual"] == 1
    alert.assert_called_once()


def test_poll_keeps_waiting_before_timeout(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("processing")
    order, _ = run(session, fz, v1, config)

    fz.get_order.return_value = fz_order("processing")
    stats = poll_upstream_orders(session, fz, v1)

    assert stats["waiting"] == 1 and order.status == OrderStatus.ORDERED_UPSTREAM


def test_giftcard_codes_are_delivered(session, config, listings):
    fz, v1 = clients(item_id=CARD_OFFER_ID)
    fz.order_giftcard.return_value = fz_order("completed", cards=[{"code": "ABCD-EFGH-1234"}])

    order, _ = run(session, fz, v1, config)

    assert order.status == OrderStatus.DELIVERED
    assert "ABCD-EFGH-1234" in order.delivered_message
    assert "Google Play" in order.delivered_message


def test_giftcard_without_codes_is_not_sent(session, config, listings):
    fz, v1 = clients(item_id=CARD_OFFER_ID)
    fz.order_giftcard.return_value = fz_order("completed")  # кодов нет

    order, alert = run(session, fz, v1, config)

    assert order.status == OrderStatus.MANUAL_REVIEW
    v1.create_message.assert_not_called()
    alert.assert_called_once()


# ----------------------------------------------------------------------
# Отказы и повторы (5.2)
# ----------------------------------------------------------------------


def test_business_error_is_not_retried(session, config, listings):
    """Пилот 01.10: 400 Insufficient balance — повторять бессмысленно."""
    fz, v1 = clients()
    fz.order_topup.side_effect = FazerCardsError(400, {"error": "Insufficient balance."})
    sleep = MagicMock()

    with patch("app.orders.order_processor.notify_admin") as alert:
        order = process_new_order(session, fz, v1, config, INVOICE, sleep=sleep)

    assert fz.order_topup.call_count == 1
    sleep.assert_not_called()
    assert order.status == OrderStatus.MANUAL_REVIEW
    assert "Insufficient balance" in order.error_message
    assert "отказал" in alert.call_args.args[0]


def test_server_errors_are_retried_then_manual(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.side_effect = FazerCardsError(503, {"error": "upstream_unavailable"})
    sleep = MagicMock()

    with patch("app.orders.order_processor.notify_admin") as alert:
        order = process_new_order(session, fz, v1, config, INVOICE, sleep=sleep)

    assert fz.order_topup.call_count == 3  # MAX_FZ_ORDER_RETRIES
    assert sleep.call_count == 2
    assert order.status == OrderStatus.MANUAL_REVIEW
    assert "upstream_unavailable" in order.error_message
    assert "недоступен" in alert.call_args.args[0]


def test_network_error_retried_with_same_idempotency_key(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.side_effect = [httpx.ConnectError("boom"), fz_order("completed")]

    order, _ = run(session, fz, v1, config)

    assert order.status == OrderStatus.DELIVERED
    keys = {call.kwargs["idempotency_key"] for call in fz.order_topup.call_args_list}
    assert keys == {INVOICE}  # повтор не задвоит заказ у FZ


def test_price_deviation_rejects_order(session, config, listings):
    fz, v1 = clients()
    fz.get_topup_offers.return_value = topup_offers(price="0.8940")  # +20%

    order, alert = run(session, fz, v1, config)

    assert order.status == OrderStatus.PRICE_REJECTED
    assert order.price_deviation_percent == Decimal("20.00")
    fz.order_topup.assert_not_called()
    alert.assert_called_once()


def test_unmappable_buyer_data_goes_manual_review(session, config, listings):
    fz, v1 = clients()
    v1.get_order_info.return_value["content"]["options"] = []

    order, alert = run(session, fz, v1, config)

    assert order.status == OrderStatus.MANUAL_REVIEW
    assert "Unique ID" in order.error_message
    fz.order_topup.assert_not_called()
    alert.assert_called_once()


def test_chat_failure_keeps_message_for_manual_delivery(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("completed")
    v1.create_message.side_effect = GGSellError(500, "oops")

    order, alert = run(session, fz, v1, config)

    assert order.status == OrderStatus.MANUAL_REVIEW
    assert "Заказ выполнен" in alert.call_args.args[0]  # текст для ручной выдачи


# ----------------------------------------------------------------------
# Идемпотентность, гонки (5.5), сверка вебхука (5.6)
# ----------------------------------------------------------------------


def test_repeat_call_does_nothing(session, config, listings):
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("completed")
    run(session, fz, v1, config)

    run(session, fz, v1, config)

    assert fz.order_topup.call_count == 1
    assert v1.create_message.call_count == 1


def test_order_already_claimed_by_another_handler(session, config, listings):
    """Второй вебхук пришёл, пока первый обрабатывает заказ: claim не удаётся."""
    fz, v1 = clients()
    fz.order_topup.return_value = fz_order("completed")
    run(session, fz, v1, config)  # регистрирует и выполняет заказ
    order = session.scalar(select(Order))
    order.status = OrderStatus.PROCESSING
    session.commit()

    run(session, fz, v1, config)

    assert fz.order_topup.call_count == 1


def test_webhook_offer_mismatch_is_rejected(session, config, listings):
    fz, v1 = clients()
    with pytest.raises(OrderProcessingError, match="в вебхуке товар"):
        run(session, fz, v1, config, expected_offer_id=999)
    fz.order_topup.assert_not_called()


def test_unpaid_order_is_rejected(session, config, listings):
    fz, v1 = clients(paid=False)
    with pytest.raises(OrderProcessingError, match="не оплачен"):
        run(session, fz, v1, config)


def test_listing_not_found_raises(session, config, listings):
    fz, v1 = clients(item_id=999999)
    with pytest.raises(OrderProcessingError):
        run(session, fz, v1, config)
