"""Синхронизация цен групповых лотов: отправка базы и надбавок на GGSell и
чтение фактических надбавок с витрины."""

from dataclasses import replace
from decimal import Decimal

import pytest

from app.clients.ggsell import GGSellError
from app.models import Listing
from app.pricing.group_price import target_price
from app.sync.showcase import push_prices, sync_group_variants
from app.tests.test_group_uploader import CONFIG, FakeGGSell, add_cards, run, session  # noqa: F401


@pytest.fixture
def lot(session):  # noqa: F811
    add_cards(session, (10, "0.21"), (50, "1.02"), (2000, "41.04"))
    v2 = FakeGGSell()
    run(session, v2)
    return session.query(Listing).one(), v2


def push(session, v2, config=CONFIG, threshold=Decimal("1")):
    alerts = []
    report = push_prices(session, v2, config, threshold_percent=threshold, pause_seconds=0,
                         sleep=lambda s: None, alert=alerts.append)
    return report, alerts


def db_totals(listing):
    return {v.position.raw_payload["name"]: v.price_rub for v in listing.variants}


def test_nothing_to_send_right_after_upload(session, lot):  # noqa: F811
    listing, v2 = lot
    calls = v2.variant_calls

    report, _ = push(session, v2)

    assert report.unchanged == 1 and report.patched == 0 and v2.variant_calls == calls


def test_markup_change_updates_base_and_all_variants(session, lot):  # noqa: F811
    listing, v2 = lot
    config = replace(CONFIG, markup_percent=Decimal("1"))

    report, _ = push(session, v2, config, threshold=Decimal(0))

    assert report.patched == 1 and report.variants_patched == 2
    assert v2.offers[listing.ggsell_offer_id]["price"] == float(listing.price_rub)
    assert v2.totals(listing.ggsell_offer_id) == db_totals(listing)
    assert all(v.price_rub == target_price(listing, v, config) for v in listing.variants)


def test_supplier_price_rise_of_one_nominal(session, lot):  # noqa: F811
    listing, v2 = lot
    base_before = v2.offers[listing.ggsell_offer_id]["price"]
    listing.variants[1].position.last_known_price_usd = Decimal("1.10")  # 50 TRY +8%
    session.commit()

    report, _ = push(session, v2)

    assert report.variants_patched == 1 and v2.offers[listing.ggsell_offer_id]["price"] == base_before
    assert v2.totals(listing.ggsell_offer_id) == db_totals(listing)


def test_manual_variant_edit_on_ggsell_read_back_and_restored(session, lot):  # noqa: F811
    listing, v2 = lot
    expected = db_totals(listing)
    edited = next(v for v in v2.variants(listing.ggsell_offer_id) if v["title_ru"] == "2000 TRY")
    edited["price"] = 10  # клиент вручную поставил надбавку 10 ₽

    synced = sync_group_variants(session, v2, sleep=lambda s: None)
    assert synced["variants_changed_on_ggsell"] == 1
    report, alerts = push(session, v2)

    assert v2.totals(listing.ggsell_offer_id) == expected == db_totals(listing)
    assert len(report.margin_fixes) == 1 and "2000 TRY" in alerts[0]


def test_partial_failure_self_heals_on_next_sync(session, lot):  # noqa: F811
    listing, v2 = lot
    config = replace(CONFIG, markup_percent=Decimal("30"))
    original = v2.create_or_update_variants
    v2.create_or_update_variants = lambda *a, **k: (_ for _ in ()).throw(GGSellError(502, "bad gateway"))

    report, _ = push(session, v2, config, threshold=Decimal(0))
    assert report.errors and v2.offers[listing.ggsell_offer_id]["price"] != float(listing.price_rub)

    # База на GGSell уже новая, надбавки — старые; следующий запуск всё выравнивает.
    v2.create_or_update_variants = original
    listing.price_rub = Decimal(str(v2.offers[listing.ggsell_offer_id]["price"]))  # как sync_listings
    session.commit()
    sync_group_variants(session, v2, sleep=lambda s: None)
    push(session, v2, config, threshold=Decimal(0))

    assert v2.totals(listing.ggsell_offer_id) == db_totals(listing)
    assert all(v.price_rub == target_price(listing, v, config) for v in listing.variants)


def test_variant_deleted_on_ggsell_marked_hidden(session, lot):  # noqa: F811
    listing, v2 = lot
    option = v2.options[listing.ggsell_offer_id][0]
    option["variants"] = [v for v in option["variants"] if v["title_ru"] != "50 TRY"]

    synced = sync_group_variants(session, v2, sleep=lambda s: None)

    assert synced["variants_hidden"] == 1
    assert {v.position.raw_payload["name"]: v.status for v in listing.variants}["50 TRY"] == "hidden"
