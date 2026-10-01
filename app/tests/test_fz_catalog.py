from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.models.base import Base
from app.models.position import Position, SourceType
from app.sync.fz_catalog import (
    import_all_giftcard_offers,
    import_all_topup_offers,
    position_name,
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
# position_name
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "category_name, offer_name, region, variant, expected",
    [
        ("Netflix", "10 USD", "US", None, "Netflix (US) — 10 USD"),
        ("Google Play (US)", "5 USD", "US", None, "Google Play (US) — 5 USD"),
        ("8 Ball Pool", "Golden Spin", "Global", None, "8 Ball Pool (Global) — Golden Spin"),
        ("Mobile Legends (RU)", "35 Diamonds", "RU", None, "Mobile Legends (RU) — 35 Diamonds"),
        ("PUBG Mobile", "60 UC", "Global", "Auto", "PUBG Mobile (Auto, Global) — 60 UC"),
        # Регион уже в названии номинала (CapCut) — в категорию его не дублируем.
        ("CapCut", "1 Month (UK) Standard", "UK", None, "CapCut — 1 Month (UK) Standard"),
        ("ExitLag Gift Card Tier 1 (Global)", "ExitLag: 1 Month Subscription", "Global", "Tier 1",
         "ExitLag Gift Card Tier 1 (Global) — ExitLag: 1 Month Subscription"),
        # Скобки категории FZ дополняем, а не ставим вторые.
        ("PUBG Mobile (Auto)", "60 UC", "Global", "Auto", "PUBG Mobile (Auto, Global) — 60 UC"),
        ("Apex Legends™ (EA)", "1000 coins", "Global", None, "Apex Legends™ (EA, Global) — 1000 coins"),
        # «US» не должно совпадать с куском слова («Plus»).
        ("Prime Plus", "1 Month", "US", None, "Prime Plus (US) — 1 Month"),
    ],
)
def test_position_name(category_name, offer_name, region, variant, expected):
    assert position_name(category_name, offer_name, region, variant) == expected


# ----------------------------------------------------------------------
# import_all_giftcard_offers
# ----------------------------------------------------------------------


def test_giftcard_import_creates_positions(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = giftcard_response()

    results = import_all_giftcard_offers(session, client, "google_play_us", region="US")

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
    assert position.region == "US"
    assert position.variant_label is None


def test_giftcard_import_adds_region_when_missing(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = giftcard_response(
        category_id="netflix_us", name="Netflix", prices={"10_usd": "10.5"}
    )

    [result] = import_all_giftcard_offers(session, client, "netflix_us", region="US")

    assert result.position.name == "Netflix (US) — 10 USD"


def test_giftcard_reimport_updates_without_duplicates(session):
    client = MagicMock()
    client.get_giftcard_offers.return_value = giftcard_response()
    import_all_giftcard_offers(session, client, "google_play_us", region="US")

    client.get_giftcard_offers.return_value = giftcard_response(
        prices={"5_usd": "5.1000", "10_usd": "9.8324"}
    )
    results = import_all_giftcard_offers(session, client, "google_play_us", region="US")

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


def test_topup_import_region(session):
    client = MagicMock()
    client.get_topup_offers.return_value = {
        "name": "Free Fire",
        "offers": [{"offer_id": "100_diamonds", "name": "100 Diamonds", "price_usd": "1.0"}],
    }

    [result] = import_all_topup_offers(session, client, "free_fire_cis", region="CIS")

    assert result.position.name == "Free Fire (CIS) — 100 Diamonds"
    assert result.position.external_id == "topup:free_fire_cis:100_diamonds"


# ----------------------------------------------------------------------
# Регион и вариант позиции (roadmap 1.9)
# ----------------------------------------------------------------------


def topup_response(name, offers, note=""):
    return {
        "name": name,
        "note": note,
        "offers": [
            {"offer_id": o.lower().replace(" ", "_"), "name": o, "price_usd": "1.0"} for o in offers
        ],
    }


def test_topup_region_from_offer_name_beats_config(session):
    """CapCut: регион у каждого номинала свой, в его названии."""
    client = MagicMock()
    client.get_topup_offers.return_value = topup_response(
        "CapCut", ["1 Month (UK) Standard", "1 Month (US) Pro"], note="CapCut top-up."
    )

    results = import_all_topup_offers(session, client, "capcut", region="Global")

    assert [r.position.region for r in results] == ["UK", "US"]
    assert results[0].position.name == "CapCut — 1 Month (UK) Standard"


def test_topup_variant_and_region_from_note(session):
    """PUBG Mobile Auto: в конфиге только вариант, регион — из примечания FZ."""
    client = MagicMock()
    client.get_topup_offers.return_value = topup_response(
        "PUBG Mobile", ["60 UC"], note="Region: Global\nPUBG Mobile top-up."
    )

    [result] = import_all_topup_offers(session, client, "pubg_mobile_auto", variant_label="Auto")

    assert result.position.region == "Global"
    assert result.position.variant_label == "Auto"
    assert result.position.name == "PUBG Mobile (Auto, Global) — 60 UC"


def test_topup_without_any_region_is_global(session):
    client = MagicMock()
    client.get_topup_offers.return_value = topup_response(
        "8 Ball Pool", ["Golden Spin"], note="8 Ball Pool top-up."
    )

    [result] = import_all_topup_offers(session, client, "8_ball_pool")

    assert result.position.region == "Global"
    assert result.position.name == "8 Ball Pool (Global) — Golden Spin"


def test_reimport_fixes_region_of_existing_position(session):
    """Повторный импорт с новым конфигом чинит уже существующие позиции:
    раньше вариант «Auto» лежал в поле региона."""
    client = MagicMock()
    client.get_topup_offers.return_value = topup_response(
        "PUBG Mobile", ["60 UC"], note="Region: Global"
    )
    import_all_topup_offers(session, client, "pubg_mobile_auto", region="Auto")

    [result] = import_all_topup_offers(session, client, "pubg_mobile_auto", variant_label="Auto")

    assert not result.created
    assert (result.position.region, result.position.variant_label) == ("Global", "Auto")
