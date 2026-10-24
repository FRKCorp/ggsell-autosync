from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Listing, ListingVariant, Position, SourceType
from app.models.base import Base
from app.offers.group_uploader import upload_groups
from app.offers.groups import GroupRule, group_positions
from app.pricing.calculator import PricingConfig

CONFIG = PricingConfig(Decimal("89.15"), Decimal("15"), Decimal("3"), Decimal("5"))
RULES = {"app_store_itunes_*": GroupRule(max_price_usd=Decimal("105"))}
CATEGORY = {"category_id": 110400, "fee": 0.06, "payment_fee": 0.027}


class FakeGGSell:
    """Офферы, параметры и варианты в памяти — как их отдаёт API GGSell."""

    def __init__(self):
        self.ids = count(1000)
        self.offers: dict[int, dict] = {}
        self.options: dict[int, list[dict]] = {}
        self.fail_variants_after_create = False
        self.variant_calls = 0

    def create_offer(self, payload):
        offer_id = next(self.ids)
        self.offers[offer_id] = dict(payload, id=offer_id, status="draft")
        self.options[offer_id] = []
        return {"data": {"id": offer_id}}

    def list_offer_options(self, offer_id):
        return {"data": [dict(o, variants=[dict(v) for v in o["variants"]]) for o in self.options[offer_id]]}

    def create_or_update_options(self, offer_id, options):
        for option in options:
            self.options[offer_id].append(dict(option, id=next(self.ids), variants=[]))
        return {"data": []}

    def create_or_update_variants(self, offer_id, option_id, variants):
        self.variant_calls += 1
        option = next(o for o in self.options[offer_id] if o["id"] == option_id)
        for variant in variants:
            option["variants"].append(dict(variant, id=next(self.ids)))
        if self.fail_variants_after_create:
            self.fail_variants_after_create = False
            raise ConnectionError("обрыв после создания вариантов")
        return {"data": []}

    def variants(self, offer_id):
        return self.options[offer_id][0]["variants"]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_cards(session, *nominals):
    cards = [Position(external_id=f"giftcard:app_store_itunes_tr:{n}_try", source_type=SourceType.GIFTCARD,
                      fz_category_id="app_store_itunes_tr", fz_offer_id=f"{n}_try", region="TR",
                      name=f"App Store & iTunes (TR) — {n} TRY", raw_payload={"name": f"{n} TRY"},
                      last_known_price_usd=Decimal(price)) for n, price in nominals]
    session.add_all(cards)
    session.commit()
    return cards


def run(session, v2, **kwargs):
    positions = session.query(Position).all()
    groups = group_positions(positions, RULES)
    category_map = {p.external_id: CATEGORY for p in positions}
    return upload_groups(session, v2, groups, category_map, CONFIG, webhook_url="https://x/webhooks/ggsell",
                         pause_seconds=0, sleep=lambda s: None, **kwargs)


def test_creates_group_offer_with_option_and_variants(session):
    add_cards(session, (10, "0.21"), (50, "1.02"), (2000, "41.04"), (5000, "110"))
    v2 = FakeGGSell()

    report = run(session, v2)

    assert report.created == ["app_store_itunes_tr"] and report.errors == {}
    listing = session.query(Listing).one()
    assert listing.is_group and listing.options_attached and listing.ggsell_option_id is not None
    offer = v2.offers[listing.ggsell_offer_id]
    assert offer["price"] == float(listing.price_rub) and "TRY" in offer["title_ru"]
    variants = v2.variants(listing.ggsell_offer_id)
    assert [v["title_ru"] for v in variants] == ["10 TRY", "50 TRY", "2000 TRY"]  # 5000 TRY дороже лимита
    assert [v["is_default"] for v in variants] == [True, False, False]
    assert variants[0]["price"] == 0
    rows = sorted(listing.variants, key=lambda v: v.sort_order)
    assert [r.ggsell_variant_id for r in rows] == [v["id"] for v in variants]
    assert all(float(r.price_rub - listing.price_rub) == v["price"] for r, v in zip(rows, variants))


def test_rerun_creates_nothing(session):
    add_cards(session, (10, "0.21"), (50, "1.02"))
    v2 = FakeGGSell()
    run(session, v2)

    report = run(session, v2)

    assert report.created == [] and report.existing == 1 and report.variants_added == {}
    assert len(v2.offers) == 1 and v2.variant_calls == 1
    assert session.query(ListingVariant).count() == 2


def test_new_fz_nominal_added_as_non_default_variant(session):
    add_cards(session, (10, "0.21"), (50, "1.02"))
    v2 = FakeGGSell()
    run(session, v2)
    add_cards(session, (100, "2.02"))

    report = run(session, v2)

    assert report.variants_added == {"app_store_itunes_tr": 1}
    listing = session.query(Listing).one()
    variants = v2.variants(listing.ggsell_offer_id)
    new = next(v for v in variants if v["title_ru"] == "100 TRY")
    assert new["is_default"] is False and new["impact_type"] == "increase" and new["price"] > 0
    assert sum(v["is_default"] for v in variants) == 1
    assert session.query(ListingVariant).count() == 3


def test_crash_after_variants_created_is_recovered_without_duplicates(session):
    add_cards(session, (10, "0.21"), (50, "1.02"))
    v2 = FakeGGSell()
    v2.fail_variants_after_create = True

    first = run(session, v2)
    assert "app_store_itunes_tr" in first.errors and session.query(ListingVariant).count() == 0

    second = run(session, v2)

    assert second.errors == {} and len(v2.offers) == 1
    listing = session.query(Listing).one()
    assert [v["title_ru"] for v in v2.variants(listing.ggsell_offer_id)] == ["10 TRY", "50 TRY"]
    assert session.query(ListingVariant).count() == 2 and listing.options_attached


def test_dry_run_touches_nothing(session):
    add_cards(session, (10, "0.21"))
    v2 = FakeGGSell()

    report = run(session, v2, dry_run=True)

    assert report.created == ["app_store_itunes_tr"] and v2.offers == {} and session.query(Listing).count() == 0


def test_group_without_nominals_or_category_skipped(session):
    add_cards(session, (5000, "110"))
    v2 = FakeGGSell()

    assert run(session, v2).empty == ["app_store_itunes_tr"]
    add_cards(session, (10, "0.21"))
    report = upload_groups(session, v2, group_positions(session.query(Position).all(), RULES), {}, CONFIG,
                           webhook_url=None, pause_seconds=0, sleep=lambda s: None)
    assert report.no_category == ["app_store_itunes_tr"] and v2.offers == {}


def test_category_with_single_lots_waits_for_conversion(session):
    cards = add_cards(session, (10, "0.21"), (50, "1.02"))
    session.add(Listing(position=cards[0], ggsell_offer_id=1, price_rub=Decimal("22"),
                        price_source_usd_at_sync=Decimal("0.21")))
    session.commit()
    v2 = FakeGGSell()

    report = run(session, v2)

    assert report.pending_conversion == ["app_store_itunes_tr"] and v2.offers == {}
    assert session.query(Listing).count() == 1
