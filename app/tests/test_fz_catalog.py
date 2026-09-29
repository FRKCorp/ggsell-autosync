from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.position import Position, SourceType
from app.sync.fz_catalog import (
    _display_category_name,
    import_all_giftcard_offers,
    import_all_topup_offers,
)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def giftcard_response(category_id="google_play_us", name="Google Play (US)", prices=None):
    """Формат ответа GET /giftcards/cards — снят с живого API 29 сентября."""
    prices = prices or {"5_usd": "4.9162", "10_usd": "9.8324"}
    return {
        "ok": True,
        "kind": "gift_card",
        "category_id": category_id,
        "name": name,
        "note": "Region: US",
        "offers": [
            {
                "card_id": card_id,
                "name": card_id.replace("_usd", " USD"),
                "price_usd": price,
                "stock": 1500,
                "min_order_quantity": 1,
                "max_order_quantity": 100,
            }
            for card_id, price in prices.items()
        ],
    }


def position_count(session):
    return session.scalar(select(func.count()).select_from(Position))


# ----------------------------------------------------------------------
# _display_category_name
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "category_name, region_label, expected",
    [
        ("Netflix", "US", "Netflix (US)"),
        ("Google Play (US)", "US", "Google Play (US)"),
        ("ExitLag Gift Card Tier 1 (Global)", "Tier 1", "ExitLag Gift Card Tier 1 (Global)"),
        ("Mobile Legends (RU)", "ru", "Mobile Legends (RU)"),
        ("Apex Legends™ (EA)", None, "Apex Legends™ (EA)"),
    ],
)
def test_display_category_name(category_name, region_label, expected):
    assert _display_category_name(category_name, region_label) == expected


# ----------------------------------------------------------------------
# import_all_giftcard_offers
# ----------------------------------------------------------------------


def test_giftcard_import_creates_positions(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = giftcard_response()

    results = import_all_giftcard_offers(session, client, "google_play_us", region_label="US")

    client.get_giftcard_offers.assert_called_once_with("google_play_us")
    assert len(results) == 2
    assert all(r.created for r in results)

    position = session.scalar(
        select(Position).where(Position.external_id == "giftcard:google_play_us:5_usd")
    )
    assert position.source_type == SourceType.GIFTCARD
    assert position.fz_category_id == "google_play_us"
    assert position.fz_offer_id == "5_usd"
    assert position.name == "Google Play (US) — 5 USD"
    assert position.last_known_price_usd == Decimal("4.9162")
    assert position.raw_payload["stock"] == 1500


def test_giftcard_import_adds_region_label_when_missing(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = giftcard_response(
        category_id="netflix_us", name="Netflix", prices={"10_usd": "10.5"}
    )

    [result] = import_all_giftcard_offers(session, client, "netflix_us", region_label="US")

    assert result.position.name == "Netflix (US) — 10 USD"


def test_giftcard_reimport_updates_without_duplicates(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = giftcard_response()
    import_all_giftcard_offers(session, client, "google_play_us", region_label="US")

    client.get_giftcard_offers.return_value = giftcard_response(
        prices={"5_usd": "5.1000", "10_usd": "9.8324"}
    )
    results = import_all_giftcard_offers(session, client, "google_play_us", region_label="US")

    assert position_count(session) == 2
    assert not any(r.created for r in results)
    by_card = {r.position.fz_offer_id: r for r in results}
    assert by_card["5_usd"].price_changed
    assert by_card["5_usd"].old_price == Decimal("4.9162")
    assert by_card["5_usd"].new_price == Decimal("5.1000")
    assert not by_card["10_usd"].price_changed


def test_giftcard_import_empty_category(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = {"ok": True, "name": "Empty", "offers": []}

    assert import_all_giftcard_offers(session, client, "empty") == []
    assert position_count(session) == 0


def test_giftcard_and_topup_with_same_ids_do_not_collide(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = giftcard_response(
        category_id="pubg_mobile", name="PUBG Mobile", prices={"60_uc": "0.9"}
    )
    client.get_topup_offers.return_value = {
        "name": "PUBG Mobile",
        "offers": [{"offer_id": "60_uc", "name": "60 UC", "price_usd": "0.8"}],
    }

    import_all_giftcard_offers(session, client, "pubg_mobile")
    import_all_topup_offers(session, client, "pubg_mobile")

    assert position_count(session) == 2


# ----------------------------------------------------------------------
# import_all_topup_offers — регрессия после выноса _display_category_name
# ----------------------------------------------------------------------


def test_topup_import_region_label(session):
    client = MagicMock()
    client.get_topup_offers.return_value = {
        "name": "Free Fire",
        "offers": [{"offer_id": "100_diamonds", "name": "100 Diamonds", "price_usd": "1.0"}],
    }

    [result] = import_all_topup_offers(session, client, "free_fire_cis", region_label="CIS")

    assert result.position.name == "Free Fire (CIS) — 100 Diamonds"
    assert result.position.external_id == "topup:free_fire_cis:100_diamonds"
