"""Заказ по групповому лоту: номинал определяется по выбранному варианту,
у FZ покупается именно он, проверки — по нему."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Listing, ListingStatus, ListingVariant, Order, OrderStatus, Position, SourceType
from app.models.base import Base
from app.orders.order_processor import poll_upstream_orders, process_new_order
from app.pricing.calculator import PricingConfig

INVOICE = "56000001"
OFFER_ID = 103721751
OPTION_ID = 8978128
CONFIG = PricingConfig(Decimal("89.15"), Decimal("15"), Decimal("3"), Decimal("5"))
CARDS = (("10_try", "10 TRY", "0.2121", "23", 72400001), ("50_try", "50 TRY", "1.0200", "110", 72400002),
         ("2000_try", "2000 TRY", "40.6119", "4326", 72400003))


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def lot(session):
    positions = [Position(external_id=f"giftcard:app_store_itunes_tr:{offer}", source_type=SourceType.GIFTCARD,
                          fz_category_id="app_store_itunes_tr", fz_offer_id=offer, region="TR",
                          name=f"App Store & iTunes (TR) — {name}", raw_payload={"name": name},
                          last_known_price_usd=Decimal(usd)) for offer, name, usd, _, _ in CARDS]
    listing = Listing(position=positions[0], ggsell_offer_id=OFFER_ID, status=ListingStatus.ACTIVE,
                      price_rub=Decimal("23"), price_source_usd_at_sync=Decimal("0.2121"), is_group=True,
                      ggsell_option_id=OPTION_ID, ggsell_fee=Decimal("0.06"), ggsell_payment_fee=Decimal("0.027"),
                      options_attached=True)
    for i, (position, (_, _, usd, rub, variant_id)) in enumerate(zip(positions, CARDS)):
        listing.variants.append(ListingVariant(position=position, ggsell_variant_id=variant_id, sort_order=i,
                                               price_rub=Decimal(rub), price_source_usd_at_sync=Decimal(usd)))
    session.add(listing)
    session.commit()
    return listing


def order_info(user_data="50 TRY", user_data_id=72400002, amount=110.0, profit=100.4):
    return {"content": {
        "item_id": OFFER_ID, "amount": amount, "profit": profit, "currency_type": "RUB",
        "date_pay": "2026-10-24T20:00:00+03:00", "cnt_goods": "1.0",
        "options": [{"id": OPTION_ID, "name": "Номинал", "user_data": user_data, "user_data_id": user_data_id}],
    }}


def fz_cards(**prices):
    offers = [{"card_id": offer, "name": name, "price_usd": prices.get(offer, usd)} for offer, name, usd, _, _ in CARDS]
    return {"name": "App Store & iTunes (TR)", "offers": offers}


def clients(info=None, **fz_prices):
    fz = MagicMock()
    fz.get_giftcard_offers.return_value = fz_cards(**fz_prices)
    fz.order_giftcard.return_value = {"ok": True, "order": {"id": "fz-1", "status": "completed", "cards": ["CODE-50"]}}
    v1 = MagicMock()
    v1.get_order_info.return_value = info or order_info()
    return fz, v1


def run(session, fz, v1):
    with patch("app.orders.order_processor.notify_admin") as alert:
        order = process_new_order(session, fz, v1, CONFIG, INVOICE, sleep=lambda s: None)
    return order, alert


def test_selected_variant_bought_and_delivered(session, lot):
    fz, v1 = clients()

    order, alert = run(session, fz, v1)

    assert order.status == OrderStatus.DELIVERED
    fz.order_giftcard.assert_called_once_with("app_store_itunes_tr", "50_try", quantity=1, idempotency_key=INVOICE)
    assert order.position.fz_offer_id == "50_try"
    assert "CODE-50" in order.delivered_message and "App Store / iTunes" in order.delivered_message
    alert.assert_not_called()


def test_nominal_found_by_title_when_id_missing(session, lot):
    fz, v1 = clients(order_info(user_data="2000 TRY", user_data_id=None, amount=4326.0, profit=3950.0))

    order, _ = run(session, fz, v1)

    assert fz.order_giftcard.call_args.args[1] == "2000_try" and order.position.fz_offer_id == "2000_try"


def test_unknown_nominal_goes_manual_without_purchase(session, lot):
    fz, v1 = clients(order_info(user_data="777 TRY", user_data_id=999))

    order, alert = run(session, fz, v1)

    assert order.status == OrderStatus.MANUAL_REVIEW and order.position_id is None
    fz.order_giftcard.assert_not_called()
    assert "не удалось определить номинал" in alert.call_args.args[0] and "777 TRY" in alert.call_args.args[0]


def test_price_check_uses_selected_nominal(session, lot):
    fz, v1 = clients(**{"50_try": "1.20"})  # 50 TRY подорожал на 18%, остальные — нет

    order, alert = run(session, fz, v1)

    assert order.status == OrderStatus.PRICE_REJECTED
    assert "кэш=1.0200" in order.error_message
    fz.order_giftcard.assert_not_called()


def test_below_cost_checked_against_selected_nominal(session, lot):
    """Покупатель заплатил цену базового номинала (23 ₽), а выбрал 50 TRY."""
    fz, v1 = clients(order_info(amount=23.0, profit=21.0))

    order, _ = run(session, fz, v1)

    assert order.status == OrderStatus.MANUAL_REVIEW and "ниже себестоимости" in order.error_message
    fz.order_giftcard.assert_not_called()


def test_poll_delivers_code_of_selected_nominal(session, lot):
    fz, v1 = clients()
    fz.order_giftcard.return_value = {"ok": True, "order": {"id": "fz-1", "status": "processing"}}
    order, _ = run(session, fz, v1)
    assert order.status == OrderStatus.ORDERED_UPSTREAM

    fz.get_order.return_value = {"ok": True, "order": {"id": "fz-1", "status": "completed", "cards": ["CODE-50"]}}
    with patch("app.orders.order_processor.notify_admin"):
        poll_upstream_orders(session, fz, v1)

    session.refresh(order)
    assert order.status == OrderStatus.DELIVERED and "CODE-50" in order.delivered_message


def test_order_row_keeps_nominal(session, lot):
    fz, v1 = clients()
    run(session, fz, v1)

    stored = session.query(Order).one()
    assert stored.position.fz_offer_id == "50_try" and stored.listing.is_group
