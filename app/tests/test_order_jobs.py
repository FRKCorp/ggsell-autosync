from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.order import Order, OrderStatus
from app.models.position import Position, SourceType
from app.orders.delivery import extract_codes
from app.orders.jobs import find_missed_invoices, find_stuck_orders, sweep_missed_orders
from app.pricing.calculator import PricingConfig

NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)
OUR_OFFER = 103310519


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        position = Position(
            external_id="topup:love_and_deepspace:60_crystals", source_type=SourceType.TOPUP,
            fz_category_id="love_and_deepspace", fz_offer_id="60_crystals", region="Global",
            name="Love and Deepspace (Global) — 60 Crystals", last_known_price_usd=Decimal("0.63"),
            raw_payload={"name": "60 Crystals"},
        )
        s.add(position)
        s.flush()
        s.add(Listing(position_id=position.id, ggsell_offer_id=OUR_OFFER, status=ListingStatus.ACTIVE,
                      price_rub=Decimal("67"), price_source_usd_at_sync=Decimal("0.63")))
        s.commit()
        yield s


def sale(invoice_id, offer_id=OUR_OFFER, hours_ago=1):
    return {"invoice_id": invoice_id, "date": (NOW - timedelta(hours=hours_ago)).isoformat(),
            "product": {"id": offer_id}}


def add_order(session, invoice_id, status, minutes_ago):
    listing = session.scalar(select(Listing))
    order = Order(ggsell_invoice_id=invoice_id, listing_id=listing.id, status=status,
                  buyer_data={}, price_at_sale_rub=Decimal("67"))
    session.add(order)
    session.commit()
    session.execute(update(Order).where(Order.id == order.id).values(updated_at=NOW - timedelta(minutes=minutes_ago)))
    session.commit()
    return order


# ----------------------------------------------------------------------
# Страховка от потерянного вебхука (5.7)
# ----------------------------------------------------------------------


def test_find_missed_invoices(session):
    add_order(session, "100", OrderStatus.DELIVERED, minutes_ago=30)
    sales = [
        sale(100),                        # уже есть у нас
        sale(101),                        # пропущен — берём
        sale(102, offer_id=51308662),     # чужой/старый лот — не наш
        sale(103, hours_ago=30),          # старше суток — не трогаем
    ]
    assert find_missed_invoices(session, sales, NOW) == [("101", OUR_OFFER)]


def test_find_stuck_orders(session):
    add_order(session, "200", OrderStatus.PENDING, minutes_ago=5)       # завис до обработки
    add_order(session, "201", OrderStatus.PENDING, minutes_ago=1)       # свежий — ещё обрабатывается
    add_order(session, "202", OrderStatus.PROCESSING, minutes_ago=15)   # процесс упал посреди
    add_order(session, "203", OrderStatus.PROCESSING, minutes_ago=3)    # идёт сейчас
    add_order(session, "204", OrderStatus.DELIVERED, minutes_ago=60)

    stuck = find_stuck_orders(session, NOW)

    assert sorted(o.ggsell_invoice_id for o in stuck) == ["200", "202"]
    statuses = dict(session.execute(select(Order.ggsell_invoice_id, Order.status)).all())
    assert statuses["202"] == OrderStatus.PENDING  # вернули, чтобы claim снова его выдал
    assert statuses["203"] == OrderStatus.PROCESSING


def test_sweep_processes_missed_sale(session):
    v1 = MagicMock()
    v1.list_last_sales.return_value = {"sales": [sale(53081130)]}
    fz = MagicMock()
    config = PricingConfig(Decimal("95"), Decimal("15"), Decimal("3"), Decimal("5"))

    with patch("app.orders.jobs.process_new_order") as process:
        stats = sweep_missed_orders(session, fz, v1, config, now=NOW)

    process.assert_called_once_with(session, fz, v1, config, "53081130", expected_offer_id=OUR_OFFER)
    assert stats == {"found": 1, "processed": 1, "errors": 0}


def test_sweep_survives_one_bad_order(session):
    from app.orders.order_processor import OrderProcessingError

    v1 = MagicMock()
    v1.list_last_sales.return_value = {"sales": [sale(1), sale(2)]}
    with patch("app.orders.jobs.process_new_order",
               side_effect=[OrderProcessingError("bad"), MagicMock()]) as process:
        stats = sweep_missed_orders(session, MagicMock(), v1, MagicMock(), now=NOW)
    assert process.call_count == 2
    assert stats == {"found": 2, "processed": 1, "errors": 1}


# ----------------------------------------------------------------------
# Вебхук — ответ сразу, обработка в фоне (5.4)
# ----------------------------------------------------------------------


@pytest.fixture
def client():
    from app.api.main import app
    return TestClient(app)


def test_webhook_answers_and_processes_in_background(client):
    body = {"id_i": 53081129, "id_d": OUR_OFFER, "amount": "10.0", "currency": "RUB",
            "SHA256": "e7d4…", "is_my_product": True}
    with patch("app.api.webhooks.process_order_in_background") as background:
        response = client.post("/webhooks/ggsell", json=body)
    assert response.status_code == 200 and response.json() == {"ok": True}
    background.assert_called_once_with("53081129", OUR_OFFER)


@pytest.mark.parametrize("body", [{}, {"id_i": "abc"}, {"id_d": 1}])
def test_webhook_without_valid_invoice_is_ignored(client, body):
    with patch("app.api.webhooks.process_order_in_background") as background:
        response = client.post("/webhooks/ggsell", json=body)
    assert response.status_code == 200
    background.assert_not_called()


# ----------------------------------------------------------------------
# Коды подарочных карт (5.3)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("fz_order, codes", [
    ({"order": {"cards": [{"code": "AAAA-BBBB"}]}}, ["AAAA-BBBB"]),
    ({"order": {"cards": ["XXXX-YYYY", "ZZZZ"]}}, ["XXXX-YYYY", "ZZZZ"]),
    ({"order": {"cards": [{"card_number": "1234", "pin": "5678"}]}}, ["Номер карты: 1234, PIN: 5678"]),
    ({"cards": [{"code": "NOWRAP"}]}, ["NOWRAP"]),
    ({"order": {"status": "completed"}}, []),
    ({"order": {"cards": [{"unknown": "x"}]}}, []),
])
def test_extract_codes(fz_order, codes):
    assert extract_codes(fz_order) == codes
