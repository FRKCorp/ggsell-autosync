"""Неожиданные ответы FazerCards: цена 0, не число, нет поля или товара,
странная структура. Требование клиента: по таким данным ничего не продавать и
не покупать, админ видит алерт, а система продолжает работать (у клиента уже
был случай, когда поставщик отдал цену 0 и лоты ушли за копейки)."""

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsError
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.order import Order, OrderStatus
from app.models.position import Position, SourceType
from app.orders.jobs import sweep_missed_orders
from app.orders.order_processor import poll_upstream_orders, process_new_order
from app.pricing.calculator import PricingConfig
from app.pricing.listing_price import listing_price_rub
from app.sync.fz_catalog import (
    UNEXPECTED_MARKER,
    UnexpectedFzResponse,
    is_gone_error,
    is_unexpected_fz_error,
    parse_fz_price,
    refresh_all_positions,
)
from app.sync.fz_special import refresh_special_positions
from app.sync.showcase import check_availability, push_prices

INVOICE = "53081129"
OFFER_ID = 103153770
FIELDS = [{"key": "user_id", "label": "Unique ID", "type": "text"}]
CONFIG = PricingConfig(Decimal("95.0"), Decimal("15.0"), Decimal("3.0"), Decimal("5.0"))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_position(session, category="8_ball_pool", offer="golden_spin", price="0.7450", listed=True,
                 offer_id=OFFER_ID):
    position = Position(
        external_id=f"topup:{category}:{offer}", source_type=SourceType.TOPUP,
        fz_category_id=category, fz_offer_id=offer, region="Global",
        name=f"{category} — {offer}", last_known_price_usd=Decimal(price), raw_payload={"name": offer},
    )
    session.add(position)
    session.flush()
    if listed:
        listing = Listing(position_id=position.id, position=position, ggsell_offer_id=offer_id,
                          status=ListingStatus.ACTIVE, price_rub=Decimal("82"), price_source_usd_at_sync=Decimal(price),
                          ggsell_fee=Decimal("0.04"), ggsell_payment_fee=Decimal("0.027"))
        if position.last_known_price_usd > 0:
            listing.price_rub = listing_price_rub(listing, CONFIG)  # на витрине — расчётная цена
        session.add(listing)
    session.commit()
    return position


def topup_offers(price="0.7450", offer="golden_spin"):
    return {"name": "8 Ball Pool", "fields": FIELDS,
            "offers": [{"offer_id": offer, "name": "Golden Spin", "price_usd": price}]}


# ----------------------------------------------------------------------
# Разбор цены
# ----------------------------------------------------------------------


@pytest.mark.parametrize("value", ["0.7450", "12", 3.5])
def test_parse_fz_price_accepts_positive_numbers(value):
    assert parse_fz_price(value) == Decimal(str(value))


@pytest.mark.parametrize("value", [0, "0", "0.00", "-1", None, "", "abc", "NaN", "Infinity", {"usd": 1}])
def test_parse_fz_price_rejects_zero_negative_and_garbage(value):
    with pytest.raises(UnexpectedFzResponse) as e:
        parse_fz_price(value)
    assert UNEXPECTED_MARKER in str(e.value)


# ----------------------------------------------------------------------
# Синхронизация цен
# ----------------------------------------------------------------------


@pytest.mark.parametrize("bad_price", ["0", 0, None, "abc", "-0.5"])
def test_sync_keeps_old_price_when_fz_returns_bad_price(session, bad_price):
    position = add_position(session)
    fz = MagicMock()
    fz.get_topup_offers.return_value = topup_offers(price=bad_price)

    (_, result, error), = refresh_all_positions(session, fz)

    assert result is None and is_unexpected_fz_error(error)
    assert not is_gone_error(error)  # это не «пропало у FZ»
    assert position.last_known_price_usd == Decimal("0.7450")


def test_sync_price_zero_does_not_touch_lot_price(session):
    """Сценарий клиента целиком: FZ отдал 0 → цена лота на GGSell не меняется."""
    add_position(session)
    fz = MagicMock()
    fz.get_topup_offers.return_value = topup_offers(price="0")
    v2 = MagicMock()

    refresh_all_positions(session, fz)
    report = push_prices(session, v2, CONFIG, threshold_percent=Decimal(0), pause_seconds=0,
                         sleep=lambda s: None, alert=lambda m: None)

    v2.patch_offer.assert_not_called()
    assert report.patched == 0


@pytest.mark.parametrize("response", [
    {"offers": [{"name": "без offer_id", "price_usd": "1"}]},  # нет ключа
    {"offers": "not a list"},
    "garbage",
    None,
])
def test_sync_survives_malformed_category_response(session, response):
    broken = add_position(session, category="broken", offer="x", offer_id=1)
    fine = add_position(session, category="8_ball_pool", offer="golden_spin", offer_id=2)
    fz = MagicMock()
    fz.get_topup_offers.side_effect = lambda category: response if category == "broken" else topup_offers("0.80")

    results = {p.external_id: (r, e) for p, r, e in refresh_all_positions(session, fz)}

    assert is_unexpected_fz_error(results[broken.external_id][1])
    assert results[fine.external_id][1] is None  # остальные категории обновились
    assert fine.last_known_price_usd == Decimal("0.80")


def test_sync_network_error_is_transient_not_unexpected(session):
    add_position(session)
    fz = MagicMock()
    fz.get_topup_offers.side_effect = httpx.ReadTimeout("timeout")

    (_, _, error), = refresh_all_positions(session, fz)

    assert "сбой связи" in error
    assert not is_unexpected_fz_error(error) and not is_gone_error(error)


def test_stars_price_zero_is_not_saved(session):
    stars = Position(external_id="telegram_stars:stars", source_type=SourceType.TELEGRAM_STARS,
                     name="Telegram (Global) — Stars", last_known_price_usd=Decimal("0.0152625"),
                     raw_payload={"min_units": 100, "max_units": 10000})
    session.add(stars)
    session.commit()
    fz = MagicMock()
    fz.get_telegram_stars.return_value = {"price_per_star": "0", "min_amount": 50, "max_amount": 10000}

    (_, result, error), = refresh_special_positions(session, fz, [stars])

    assert result is None and is_unexpected_fz_error(error)
    assert stars.last_known_price_usd == Decimal("0.0152625")


def test_unexpected_answer_alerts_admin_for_listed_lots_only(session):
    listed = add_position(session, offer="a", offer_id=1)
    unlisted = add_position(session, offer="b", listed=False)
    error = f"{UNEXPECTED_MARKER}: цена '0'"
    alerts = []

    result = check_availability(session, [(listed, None, error), (unlisted, None, error)],
                                now=datetime.now(timezone.utc), alert=alerts.append)

    assert result["unexpected"] == 1
    assert len(alerts) == 1 and "неправильные данные" in alerts[0] and listed.name in alerts[0]
    assert listed.fz_missing_since is None  # не «пропало»


# ----------------------------------------------------------------------
# Заказ: проверка цены перед закупкой
# ----------------------------------------------------------------------


def order_clients(fz_offers=None):
    fz = MagicMock()
    fz.get_topup_offers.return_value = fz_offers if fz_offers is not None else topup_offers()
    fz.order_topup.return_value = {"ok": True, "order": {"id": "ord-1", "status": "completed"}}
    v1 = MagicMock()
    v1.get_order_info.return_value = {"content": {
        "item_id": OFFER_ID, "amount": 82.0, "profit": 78.15, "date_pay": "2026-10-01T17:47:37+03:00",
        "options": [{"id": 1, "name": "Unique ID", "user_data": "player123", "user_data_id": 1}],
    }}
    return fz, v1


def run_order(session, fz, v1, sleep=None):
    with patch("app.orders.order_processor.notify_admin") as alert:
        order = process_new_order(session, fz, v1, CONFIG, INVOICE, sleep=sleep or (lambda s: None))
    return order, alert


def assert_manual_without_purchase(order, alert, fz, text):
    assert order.status == OrderStatus.MANUAL_REVIEW
    fz.order_topup.assert_not_called()
    alert.assert_called_once()
    assert "НЕ сделан" in alert.call_args.args[0] and text in alert.call_args.args[0]


def test_order_when_cached_price_is_zero_goes_manual(session):
    add_position(session, price="0")
    fz, v1 = order_clients()

    order, alert = run_order(session, fz, v1)

    assert_manual_without_purchase(order, alert, fz, "нет нормальной цены")


@pytest.mark.parametrize("live_price", ["0", None, "abc", "-3"])
def test_order_when_fz_live_price_is_bad_goes_manual(session, live_price):
    add_position(session)
    fz, v1 = order_clients(topup_offers(price=live_price))

    order, alert = run_order(session, fz, v1)

    assert_manual_without_purchase(order, alert, fz, UNEXPECTED_MARKER)


def test_order_when_item_missing_in_fz_answer_goes_manual(session):
    add_position(session)
    fz, v1 = order_clients(topup_offers(offer="other_offer"))

    order, alert = run_order(session, fz, v1)

    assert_manual_without_purchase(order, alert, fz, "нет в ответе FZ")


@pytest.mark.parametrize("answer", [{"no_offers": []}, "garbage", {"offers": [{"price_usd": "1"}]}])
def test_order_when_fz_answer_is_malformed_goes_manual(session, answer):
    add_position(session)
    fz, v1 = order_clients(answer)

    order, alert = run_order(session, fz, v1)

    assert_manual_without_purchase(order, alert, fz, UNEXPECTED_MARKER)


def test_order_price_check_retries_transient_fz_errors(session):
    add_position(session)
    fz, v1 = order_clients()
    fz.get_topup_offers.side_effect = [FazerCardsError(502, {"error": "bad_gateway"}), topup_offers()]
    sleep = MagicMock()

    order, alert = run_order(session, fz, v1, sleep=sleep)

    assert order.status == OrderStatus.DELIVERED
    assert sleep.call_count == 1
    alert.assert_not_called()


def test_order_price_check_fz_down_goes_manual_after_retries(session):
    add_position(session)
    fz, v1 = order_clients()
    fz.get_topup_offers.side_effect = httpx.ConnectError("refused")

    order, alert = run_order(session, fz, v1)

    assert fz.get_topup_offers.call_count == 3
    assert_manual_without_purchase(order, alert, fz, "FazerCards недоступен")


def test_order_price_check_business_error_goes_manual_without_retries(session):
    add_position(session)
    fz, v1 = order_clients()
    fz.get_topup_offers.side_effect = FazerCardsError(403, {"error": "Subscription is not active"})

    order, alert = run_order(session, fz, v1)

    assert fz.get_topup_offers.call_count == 1
    assert_manual_without_purchase(order, alert, fz, "Subscription is not active")


# ----------------------------------------------------------------------
# Заказ: ответ FZ на покупку и опрос статуса
# ----------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["garbage", {"ok": True, "order": "garbage"}])
def test_unreadable_answer_to_purchase_goes_manual_with_warning(session, answer):
    add_position(session)
    fz, v1 = order_clients()
    fz.order_topup.return_value = answer

    order, alert = run_order(session, fz, v1)

    assert order.status == OrderStatus.MANUAL_REVIEW
    assert "МОГ пройти" in alert.call_args.args[0]
    v1.create_message.assert_not_called()


def test_poll_unreadable_answer_goes_manual_and_others_continue(session):
    add_position(session, offer="a", offer_id=1)
    add_position(session, offer="b", offer_id=2)
    listings = session.query(Listing).order_by(Listing.id).all()
    for i, listing in enumerate(listings):
        session.add(Order(ggsell_invoice_id=f"100{i}", listing_id=listing.id, status=OrderStatus.ORDERED_UPSTREAM,
                          buyer_data={"Unique ID": "p1"}, price_at_sale_rub=Decimal("82"), fz_order_id=f"fz-{i}",
                          fz_ordered_at=datetime.now(timezone.utc)))
    session.commit()
    fz = MagicMock()
    fz.get_order.side_effect = lambda order_id: (
        "garbage" if order_id == "fz-0" else {"ok": True, "order": {"id": order_id, "status": "completed"}}
    )
    v1 = MagicMock()

    with patch("app.orders.order_processor.notify_admin") as alert:
        stats = poll_upstream_orders(session, fz, v1)

    assert stats["manual"] == 1 and stats["delivered"] == 1
    assert "МОГ пройти" in alert.call_args.args[0]
    first = session.query(Order).filter_by(ggsell_invoice_id="1000").one()
    assert first.fz_order_id == "fz-0"  # номер заказа FZ сохранён для разбора


def test_sweep_one_broken_order_does_not_stop_others(session):
    v1 = MagicMock()
    v1.list_last_sales.return_value = {"sales": []}
    with patch("app.orders.jobs.find_stuck_orders",
               return_value=[MagicMock(ggsell_invoice_id="bad"), MagicMock(ggsell_invoice_id="good")]), \
         patch("app.orders.jobs.process_new_order", side_effect=[RuntimeError("bug"), None]) as process:
        stats = sweep_missed_orders(session, MagicMock(), v1, CONFIG)

    assert process.call_count == 2
    assert stats == {"found": 2, "processed": 1, "errors": 1}
