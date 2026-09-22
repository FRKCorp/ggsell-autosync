from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsError
from app.clients.ggsell import GGSellError
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.order import OrderStatus
from app.models.position import Position, SourceType
from app.orders.order_processor import process_new_order
from app.pricing.calculator import PricingConfig


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
def position_and_listing(session):
    position = Position(
        external_id="topup:8_ball_pool:golden_spin",
        source_type=SourceType.TOPUP,
        fz_category_id="8_ball_pool",
        fz_offer_id="golden_spin",
        name="8 Ball Pool — Golden Spin",
        last_known_price_usd=Decimal("0.7450"),
        raw_payload={},
    )
    session.add(position)
    session.flush()

    listing = Listing(
        position_id=position.id,
        ggsell_offer_id=103153770,
        status=ListingStatus.ACTIVE,
        price_rub=Decimal("81.40"),
        price_source_usd_at_sync=Decimal("0.7450"),
    )
    session.add(listing)
    session.commit()
    return position, listing


def _order_info_response(item_id: int, user_data: str = "player123") -> dict:
    return {
        "content": {
            "item_id": item_id,
            "options": [
                {"id": 1, "name": "user_id", "user_data": user_data, "user_data_id": 1}
            ],
        }
    }


def test_happy_path_delivers_order(session, config, position_and_listing):
    position, listing = position_and_listing

    fz_client = MagicMock()
    fz_client.get_topup_offers.return_value = {
        "name": "8 Ball Pool",
        "offers": [{"offer_id": "golden_spin", "name": "Golden Spin", "price_usd": "0.7450"}],
    }
    fz_client.order_topup.return_value = {"order_id": "fz-order-1", "status": "completed"}

    ggsell_v1 = MagicMock()
    ggsell_v1.get_order_info.return_value = _order_info_response(listing.ggsell_offer_id)

    order = process_new_order(session, fz_client, ggsell_v1, config, invoice_id="1001")

    # chat_id сейчас всегда None (TODO не решён) -> ожидаем MANUAL_REVIEW,
    # но товар должен быть УЖЕ заказан у FZ (ORDERED_UPSTREAM пройден).
    assert order.fz_order_id == "fz-order-1"
    assert order.status == OrderStatus.MANUAL_REVIEW
    fz_client.order_topup.assert_called_once()


def test_price_deviation_rejects_order(session, config, position_and_listing):
    position, listing = position_and_listing

    fz_client = MagicMock()
    # Живая цена на 20% выше кэшированной 0.7450 -> превышает порог 5%.
    fz_client.get_topup_offers.return_value = {
        "name": "8 Ball Pool",
        "offers": [{"offer_id": "golden_spin", "name": "Golden Spin", "price_usd": "0.8940"}],
    }

    ggsell_v1 = MagicMock()
    ggsell_v1.get_order_info.return_value = _order_info_response(listing.ggsell_offer_id)

    order = process_new_order(session, fz_client, ggsell_v1, config, invoice_id="1002")

    assert order.status == OrderStatus.PRICE_REJECTED
    fz_client.order_topup.assert_not_called()


def test_fz_failure_after_retries_goes_manual_review(session, config, position_and_listing):
    position, listing = position_and_listing

    fz_client = MagicMock()
    fz_client.get_topup_offers.return_value = {
        "name": "8 Ball Pool",
        "offers": [{"offer_id": "golden_spin", "name": "Golden Spin", "price_usd": "0.7450"}],
    }
    fz_client.order_topup.side_effect = FazerCardsError(503, {"error": "upstream_unavailable"})

    ggsell_v1 = MagicMock()
    ggsell_v1.get_order_info.return_value = _order_info_response(listing.ggsell_offer_id)

    with patch("app.orders.order_processor.time.sleep"):  # не ждать реальные 5с*2 в тесте
        order = process_new_order(session, fz_client, ggsell_v1, config, invoice_id="1003")

    assert order.status == OrderStatus.MANUAL_REVIEW
    assert fz_client.order_topup.call_count == 3  # MAX_FZ_ORDER_RETRIES
    assert "upstream_unavailable" in order.error_message


def test_idempotent_on_repeat_call(session, config, position_and_listing):
    """Повторный вызов с тем же invoice_id для уже обработанного заказа не
    должен снова дёргать FazerCards."""
    position, listing = position_and_listing

    fz_client = MagicMock()
    fz_client.get_topup_offers.return_value = {
        "name": "8 Ball Pool",
        "offers": [{"offer_id": "golden_spin", "name": "Golden Spin", "price_usd": "0.7450"}],
    }
    fz_client.order_topup.return_value = {"order_id": "fz-order-2"}

    ggsell_v1 = MagicMock()
    ggsell_v1.get_order_info.return_value = _order_info_response(listing.ggsell_offer_id)

    process_new_order(session, fz_client, ggsell_v1, config, invoice_id="1004")
    assert fz_client.order_topup.call_count == 1

    # Второй вызов с тем же invoice_id — статус уже не PENDING, повторной
    # обработки быть не должно.
    process_new_order(session, fz_client, ggsell_v1, config, invoice_id="1004")
    assert fz_client.order_topup.call_count == 1


def test_listing_not_found_raises(session, config, position_and_listing):
    from app.orders.order_processor import OrderProcessingError

    ggsell_v1 = MagicMock()
    ggsell_v1.get_order_info.return_value = _order_info_response(item_id=999999)

    fz_client = MagicMock()

    with pytest.raises(OrderProcessingError):
        process_new_order(session, fz_client, ggsell_v1, config, invoice_id="1005")
