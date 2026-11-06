"""Авторежим наличия: пропавшее у FZ снимается с продажи, вернувшееся — возвращается."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.clients.ggsell import GGSellError
from app.models import Listing, ListingStatus, Position, SourceType
from app.models.base import Base
from app.sync.availability import UNAVAILABLE, apply_availability
from app.sync.showcase import check_availability, sync_group_variants
from app.tests.test_group_conversion import FakeBatchGGSell
from app.tests.test_group_uploader import CONFIG, add_cards, run as upload

GONE_AT = datetime(2026, 11, 6, tzinfo=timezone.utc)


class FakeShowcase(FakeBatchGGSell):
    """Варианты как у GGSell 06.11: archived исчезает из списка, менять
    архивный или скрытый вариант нельзя — 404."""

    def create_or_update_variants(self, offer_id, option_id, variants):
        self.variant_calls += 1
        option = next(o for o in self.options[offer_id] if o["id"] == option_id)
        for variant in variants:
            if "id" not in variant:
                option["variants"].append(dict(variant, id=next(self.ids)))
                continue
            existing = next((v for v in option["variants"] if v["id"] == variant["id"]), None)
            if existing is None or existing.get("status") == "hidden":
                raise GGSellError(404, {"errors": [{"code": "NOT_FOUND"}]})
            existing.update(variant)
            if existing["status"] == "archived":
                option["variants"].remove(existing)
        return {"data": []}

    def batch_activate_offers(self, offer_ids):
        return self._job(offer_ids, "active")

    def defaults(self, offer_id):
        return [v["title_ru"] for v in self.variants(offer_id) if v["is_default"]]


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


@pytest.fixture
def group(session):
    """Групповой лот Турции: 10, 50, 100 TRY (база — 10 TRY)."""
    v2 = FakeShowcase()
    cards = add_cards(session, (10, "0.21"), (50, "1.02"), (100, "2.02"))
    upload(session, v2)
    listing = session.query(Listing).one()
    return v2, listing, {p.fz_offer_id: p for p in cards}


def gone(session, *positions):
    for position in positions:
        position.fz_missing_since = GONE_AT
    session.commit()


def back(session, *positions):
    for position in positions:
        position.fz_missing_since = None
    session.commit()


def apply(session, v2):
    return apply_availability(session, v2, CONFIG, sleep=lambda s: None)


def assert_consistent(v2, listing):
    """Цены на витрине = цены в БД, ровно один вариант по умолчанию — базовый."""
    totals = v2.totals(listing.ggsell_offer_id)
    active = {v.position.raw_payload["name"]: v.price_rub for v in listing.variants if v.status == "active"}
    assert totals == active
    base_title = listing.position.raw_payload["name"]
    assert v2.defaults(listing.ggsell_offer_id) == [base_title]
    assert v2.offers[listing.ggsell_offer_id]["price"] == float(listing.price_rub) == float(active[base_title])


def test_base_nominal_gone_next_one_becomes_base(session, group):
    v2, listing, cards = group
    gone(session, cards["10_try"])

    report = apply(session, v2)

    assert report.hidden == ["App Store & iTunes (TR) — 10 TRY"] and report.errors == {}
    assert [v["title_ru"] for v in v2.variants(listing.ggsell_offer_id)] == ["50 TRY", "100 TRY"]
    assert listing.position.fz_offer_id == "50_try"
    assert next(v for v in listing.variants if v.position.fz_offer_id == "10_try").status == UNAVAILABLE
    assert_consistent(v2, listing)


def test_base_nominal_back_restored_as_new_variant(session, group):
    v2, listing, cards = group
    old_id = next(v.ggsell_variant_id for v in listing.variants if v.position.fz_offer_id == "10_try")
    gone(session, cards["10_try"])
    apply(session, v2)
    back(session, cards["10_try"])

    report = apply(session, v2)

    assert report.restored == ["App Store & iTunes (TR) — 10 TRY"] and report.errors == {}
    restored = next(v for v in listing.variants if v.position.fz_offer_id == "10_try")
    assert restored.status == "active" and restored.ggsell_variant_id not in (None, old_id)
    assert listing.position.fz_offer_id == "10_try"
    assert_consistent(v2, listing)


def test_middle_nominal_gone_base_untouched(session, group):
    v2, listing, cards = group
    price_before = v2.offers[listing.ggsell_offer_id]["price"]
    patches = len(v2.patches)
    gone(session, cards["50_try"])

    apply(session, v2)

    assert [v["title_ru"] for v in v2.variants(listing.ggsell_offer_id)] == ["10 TRY", "100 TRY"]
    assert v2.offers[listing.ggsell_offer_id]["price"] == price_before and len(v2.patches) == patches
    assert_consistent(v2, listing)


def test_nothing_changes_nothing_sent(session, group):
    v2, listing, _ = group
    calls = v2.variant_calls

    report = apply(session, v2)

    assert v2.variant_calls == calls and report.summary().startswith("номиналов скрыто=0")
    assert report.alert_text() is None


def test_all_nominals_gone_lot_paused_then_activated(session, group):
    v2, listing, cards = group
    listing.status = ListingStatus.ACTIVE
    v2.offers[listing.ggsell_offer_id]["status"] = "active"
    gone(session, *cards.values())

    report = apply(session, v2)

    assert report.paused == ["App Store & iTunes (TR)"] and listing.auto_paused
    assert listing.status == ListingStatus.PAUSED and v2.offers[listing.ggsell_offer_id]["status"] == "paused"
    assert len(v2.variants(listing.ggsell_offer_id)) == 3  # варианты не трогали

    back(session, *cards.values())
    report = apply(session, v2)

    assert report.activated == ["App Store & iTunes (TR)"] and not listing.auto_paused
    assert v2.offers[listing.ggsell_offer_id]["status"] == "active"


def test_variant_hidden_by_hand_not_restored(session, group):
    v2, listing, _ = group
    manual = next(v for v in listing.variants if v.position.fz_offer_id == "100_try")
    next(v for v in v2.variants(listing.ggsell_offer_id) if v["id"] == manual.ggsell_variant_id)["status"] = "hidden"
    sync_group_variants(session, v2, sleep=lambda s: None)
    assert manual.status == "hidden"

    apply(session, v2)

    assert manual.status == "hidden" and len(v2.variants(listing.ggsell_offer_id)) == 3


def test_nominal_over_limit_hidden(session, group):
    v2, listing, cards = group
    cards["100_try"].last_known_price_usd = Decimal("120")  # дороже лимита $105
    session.commit()

    report = apply(session, v2)

    assert report.hidden == ["App Store & iTunes (TR) — 100 TRY"]
    assert [v["title_ru"] for v in v2.variants(listing.ggsell_offer_id)] == ["10 TRY", "50 TRY"]


def test_showcase_sync_keeps_our_unavailable_status(session, group):
    v2, listing, cards = group
    gone(session, cards["50_try"])
    apply(session, v2)

    sync_group_variants(session, v2, sleep=lambda s: None)

    assert next(v for v in listing.variants if v.position.fz_offer_id == "50_try").status == UNAVAILABLE


def test_back_alert_for_nominal_we_hid(session, group):
    v2, listing, cards = group
    gone(session, cards["50_try"])
    apply(session, v2)
    alerts = []

    check_availability(session, [(cards["50_try"], object(), None)], alert=alerts.append, auto=True)

    assert alerts and "Снова есть" in alerts[0] and "50 TRY" in alerts[0]


def test_gone_alert_wording_in_auto_mode(session, group):
    v2, listing, cards = group
    alerts = []

    check_availability(session, [(cards["50_try"], None, "card_id='50_try' больше не найден")],
                       alert=alerts.append, auto=True)

    assert "снимаются с продажи автоматически" in alerts[0]


def single_lot(session, v2, status=ListingStatus.ACTIVE, auto_paused=False):
    position = Position(external_id="topup:mlbb:275", source_type=SourceType.TOPUP, name="Mobile Legends — 275",
                        last_known_price_usd=Decimal("4.5"), raw_payload={"name": "275"})
    offer_id = v2.add_offer("Mobile Legends 275", 450, status)
    listing = Listing(position=position, ggsell_offer_id=offer_id, status=status, price_rub=Decimal(450),
                      price_source_usd_at_sync=Decimal("4.5"), auto_paused=auto_paused)
    session.add(listing)
    session.commit()
    return listing


def test_single_lot_paused_and_activated():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        v2 = FakeShowcase()
        listing = single_lot(session, v2)
        gone(session, listing.position)

        report = apply(session, v2)
        assert report.paused == ["Mobile Legends — 275"] and listing.auto_paused
        assert v2.offers[listing.ggsell_offer_id]["status"] == "paused"

        back(session, listing.position)
        report = apply(session, v2)
        assert report.activated == ["Mobile Legends — 275"] and not listing.auto_paused
        assert v2.offers[listing.ggsell_offer_id]["status"] == "active"


def test_lot_paused_by_hand_stays_paused():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        v2 = FakeShowcase()
        listing = single_lot(session, v2, status=ListingStatus.PAUSED)

        report = apply(session, v2)

        assert report.activated == [] and v2.offers[listing.ggsell_offer_id]["status"] == "paused"


def test_draft_lot_not_paused():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        v2 = FakeShowcase()
        listing = single_lot(session, v2, status=ListingStatus.DRAFT)
        gone(session, listing.position)

        report = apply(session, v2)

        assert report.paused == [] and v2.jobs == {}
