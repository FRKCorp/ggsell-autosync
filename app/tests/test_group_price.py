from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Listing, ListingStatus, ListingVariant, Position, SourceType
from app.models.base import Base
from app.pricing.calculator import PricingConfig
from app.pricing.group_price import apply_group_update, plan_group_update, target_price, variant_payload

CONFIG = PricingConfig(Decimal("89.15"), Decimal("15"), Decimal("3"), Decimal("5"))
ONE = Decimal("1")


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def lot(session):
    """App Store RU: 500 / 1000 / 7000 RUB, цены на витрине = расчётные."""
    cards = [Position(external_id=f"giftcard:app_store_itunes_ru:{n}_rub", source_type=SourceType.GIFTCARD,
                      fz_category_id="app_store_itunes_ru", fz_offer_id=f"{n}_rub", region="RU",
                      name=f"App Store & iTunes (RU) — {n} RUB", raw_payload={"name": f"{n} RUB"},
                      last_known_price_usd=Decimal(p))
             for n, p in ((500, "7.3764"), (1000, "14.7528"), (7000, "103.82"))]
    listing = Listing(position=cards[0], ggsell_offer_id=1, status=ListingStatus.ACTIVE, price_rub=Decimal(0),
                      price_source_usd_at_sync=cards[0].last_known_price_usd, is_group=True, ggsell_option_id=10,
                      ggsell_fee=Decimal("0.06"), ggsell_payment_fee=Decimal("0.027"))
    for i, card in enumerate(cards):
        listing.variants.append(ListingVariant(position=card, ggsell_variant_id=100 + i, sort_order=i,
                                               price_rub=Decimal(0), price_source_usd_at_sync=card.last_known_price_usd))
    session.add(listing)
    session.flush()
    for variant in listing.variants:
        variant.price_rub = target_price(listing, variant, CONFIG)
    listing.price_rub = listing.variants[0].price_rub
    session.commit()
    return listing


def plan(listing, config=CONFIG, threshold=ONE):
    return plan_group_update(listing, config, threshold)


def deltas(listing):
    return [v.price_rub - listing.price_rub for v in listing.variants]


def test_variant_payload_increase_and_decrease():
    assert variant_payload(7, Decimal("1500"), Decimal("813")) == {
        "id": 7, "price": 687.0, "discount_type": "fixed", "impact_type": "increase"}
    assert variant_payload(None, Decimal("700"), Decimal("813"))["impact_type"] == "decrease"
    assert variant_payload(None, Decimal("700"), Decimal("813"))["price"] == 113.0
    assert "id" not in variant_payload(None, Decimal("1"), Decimal("1"))


def test_nothing_changed(lot):
    update = plan(lot)

    assert update.empty and update.prices == {}


def test_one_nominal_rises_only_its_variant_sent(lot):
    middle = lot.variants[1]
    middle.position.last_known_price_usd = Decimal("15.50")  # +5%

    update = plan(lot)

    assert not update.base_changed
    new = target_price(lot, middle, CONFIG)
    assert update.prices == {middle.id: new}
    assert update.variants == [variant_payload(101, new, lot.price_rub)]


def test_small_change_below_threshold_skipped(lot):
    lot.variants[2].position.last_known_price_usd = Decimal("103.90")  # +0.08%

    assert plan(lot).empty


def test_base_nominal_change_shifts_all_deltas(lot):
    old_prices = [v.price_rub for v in lot.variants]
    lot.variants[0].position.last_known_price_usd = Decimal("8.20")  # база +11%

    update = plan(lot)

    assert update.base_changed and update.base_price > old_prices[0]
    # Цены остальных номиналов не изменились, а надбавки — да: база выросла.
    assert [p["id"] for p in update.variants] == [101, 102]
    assert all(p["price"] == float(old - update.base_price) for p, old in zip(update.variants, old_prices[1:]))


def test_markup_change_reprices_everything(lot):
    update = plan(lot, replace(CONFIG, markup_percent=Decimal("1")), threshold=Decimal(0))

    assert update.base_changed and len(update.prices) == 3
    apply_group_update(lot, update)
    assert all(v.price_rub == target_price(lot, v, replace(CONFIG, markup_percent=Decimal("1"))) for v in lot.variants)


def test_unsafe_margin_repriced_despite_threshold(lot):
    top = lot.variants[2]
    top.position.last_known_price_usd = Decimal("120")  # +15.6% — наценка съедена

    update = plan(lot, threshold=Decimal("50"))

    assert top.id in update.prices and len(update.margin_fixes) == 1


def test_nominal_cheaper_than_base_gets_decrease(lot):
    lot.variants[1].position.last_known_price_usd = Decimal("5")

    update = plan(lot)

    assert update.variants == [variant_payload(101, update.prices[lot.variants[1].id], lot.price_rub)]
    assert update.variants[0]["impact_type"] == "decrease"


def test_missing_and_hidden_nominals_left_alone(lot):
    lot.variants[1].position.fz_missing_since = datetime(2026, 10, 24, tzinfo=timezone.utc)
    lot.variants[1].position.last_known_price_usd = Decimal("30")
    lot.variants[2].status = "hidden"
    lot.variants[2].position.last_known_price_usd = Decimal("50")

    assert plan(lot).empty


def test_variant_not_yet_on_ggsell_priced_but_not_sent(lot):
    lot.variants[2].ggsell_variant_id = None
    lot.variants[2].position.last_known_price_usd = Decimal("90")

    update = plan(lot)

    assert lot.variants[2].id in update.prices and update.variants == []


def test_apply_writes_prices_and_keeps_deltas_consistent(lot):
    lot.variants[0].position.last_known_price_usd = Decimal("8.20")
    lot.variants[2].position.last_known_price_usd = Decimal("98")

    update = plan(lot)
    apply_group_update(lot, update)

    assert lot.price_rub == lot.variants[0].price_rub == update.base_price
    assert lot.price_source_usd_at_sync == Decimal("8.20")
    sent = {p["id"]: p for p in update.variants}
    for v in lot.variants[1:]:
        payload = sent.get(v.ggsell_variant_id)
        assert payload is not None and payload["price"] == float(v.price_rub - lot.price_rub)
    assert plan(lot).empty  # после записи — снова нечего отправлять
