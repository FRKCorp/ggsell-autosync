from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import settings
from app.clients.ggsell import GGSellError
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.position import Position, SourceType
from app.pricing.calculator import PricingConfig
from app.pricing.listing_price import listing_price_rub
from app.sync import jobs
from app.sync.fz_catalog import is_gone_error
from app.sync.showcase import check_availability, fetch_offers, push_prices, sync_listings

CONFIG = PricingConfig(Decimal("87.7367"), Decimal("15"), Decimal("3"), Decimal("5"))
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
GONE = "offer_id='275' больше не найден в категории 'mlbb_ru'"
TRANSIENT = "FazerCards error 503: upstream unavailable"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_listing(session, external_id, price_usd, offer_id, *, source=SourceType.TOPUP, stock=None,
                status=ListingStatus.DRAFT, markup=None, listed=True):
    raw = {"name": external_id} | ({"stock": stock} if stock is not None else {})
    position = Position(external_id=external_id, source_type=source, name=external_id,
                        last_known_price_usd=Decimal(price_usd), raw_payload=raw)
    session.add(position)
    session.flush()
    if not listed:
        session.commit()
        return position, None
    listing = Listing(position=position, ggsell_offer_id=offer_id, status=status, price_rub=Decimal(0),
                      price_source_usd_at_sync=position.last_known_price_usd,
                      ggsell_fee=Decimal("0.04"), ggsell_payment_fee=Decimal("0.027"), markup_percent=markup)
    listing.price_rub = listing_price_rub(listing, CONFIG)  # цена на витрине = расчётной
    session.add(listing)
    session.commit()
    return position, listing


def run_push(session, v2, **kwargs):
    alerts = []
    kwargs.setdefault("threshold_percent", Decimal("1"))
    report = push_prices(session, v2, CONFIG, pause_seconds=0, sleep=lambda s: None,
                         alert=alerts.append, **kwargs)
    return report, alerts


# ----------------------------------------------------------------------
# 4.1, 4.2 — цены
# ----------------------------------------------------------------------


def test_price_unchanged_no_patch(session):
    add_listing(session, "a", "4.6857", 1)
    v2 = MagicMock()

    report, alerts = run_push(session, v2)

    assert report.unchanged == 1 and report.patched == 0
    v2.patch_offer.assert_not_called()
    assert alerts == []


def test_small_change_below_threshold_skipped(session):
    position, listing = add_listing(session, "a", "50", 1)
    old = listing.price_rub
    position.last_known_price_usd = Decimal("50.2")  # +0.4%
    session.commit()
    v2 = MagicMock()

    report, _ = run_push(session, v2)

    assert report.skipped_below_threshold == 1
    v2.patch_offer.assert_not_called()
    assert listing.price_rub == old


def test_change_above_threshold_patches_only_price_and_updates_listing(session):
    position, listing = add_listing(session, "a", "4.6857", 1)
    position.last_known_price_usd = Decimal("4.80")  # +2.4%
    session.commit()
    expected = listing_price_rub(listing, CONFIG)
    v2 = MagicMock()

    report, alerts = run_push(session, v2)

    assert report.patched == 1
    v2.patch_offer.assert_called_once_with(1, {"price": float(expected)})
    assert listing.price_rub == expected
    assert listing.price_source_usd_at_sync == Decimal("4.80")
    assert alerts == []  # маржа не падала ниже минимума — алерт не нужен


def test_price_drop_also_pushed(session):
    position, listing = add_listing(session, "a", "10", 1)
    old = listing.price_rub
    position.last_known_price_usd = Decimal("9")
    session.commit()
    v2 = MagicMock()

    report, _ = run_push(session, v2)

    assert report.patched == 1 and listing.price_rub < old


def test_unsafe_margin_pushed_despite_threshold_with_alert(session):
    position, listing = add_listing(session, "a", "10", 1)
    position.last_known_price_usd = Decimal("11.5")  # +15% — наценка 15% съедена
    session.commit()
    v2 = MagicMock()

    report, alerts = run_push(session, v2, threshold_percent=Decimal("50"))

    assert report.patched == 1 and len(report.margin_fixes) == 1
    assert len(alerts) == 1 and "маржу ниже 3%" in alerts[0] and "a:" in alerts[0]


def test_global_and_individual_markup_change(session):
    _, plain = add_listing(session, "a", "10", 1)
    _, custom = add_listing(session, "b", "10", 2, markup=Decimal("15"))
    v2 = MagicMock()

    # Глобальная наценка 15 → 25: меняется только лот без индивидуальной.
    report = push_prices(session, v2, PricingConfig(Decimal("87.7367"), Decimal("25"), Decimal("3"), Decimal("5")),
                         threshold_percent=Decimal("1"), pause_seconds=0, sleep=lambda s: None, alert=lambda m: None)
    assert report.patched == 1
    assert v2.patch_offer.call_args.args[0] == 1

    # Индивидуальная наценка лота b → 30.
    custom.markup_percent = Decimal("30")
    session.commit()
    v2.reset_mock()
    report, _ = run_push(session, v2)
    assert [c.args[0] for c in v2.patch_offer.call_args_list] == [1, 2]  # 1 — обратно на 15%, 2 — на 30%
    assert custom.price_rub > plain.price_rub


def test_deleted_on_ggsell_marked_archived(session):
    position, listing = add_listing(session, "a", "10", 1)
    position.last_known_price_usd = Decimal("12")
    session.commit()
    v2 = MagicMock()
    v2.patch_offer.side_effect = GGSellError(404, {"error": "not found"})

    report, _ = run_push(session, v2)

    assert report.deleted_on_ggsell == 1
    assert listing.status == ListingStatus.ARCHIVED


def test_server_errors_retried_then_reported(session):
    position, listing = add_listing(session, "a", "10", 1)
    old = listing.price_rub
    position.last_known_price_usd = Decimal("12")
    session.commit()
    v2 = MagicMock()
    v2.patch_offer.side_effect = GGSellError(502, "bad gateway")

    report, alerts = run_push(session, v2)

    assert v2.patch_offer.call_count == 4  # call_with_retry
    assert report.errors == {1: "502: bad gateway"}
    assert listing.price_rub == old
    assert len(alerts) == 1 and "Не удалось обновить цену у 1" in alerts[0]


def test_429_retried_then_succeeds(session):
    position, listing = add_listing(session, "a", "10", 1)
    position.last_known_price_usd = Decimal("12")
    session.commit()
    v2 = MagicMock()
    v2.patch_offer.side_effect = [GGSellError(429, "slow down"), {"data": {}}]

    report, _ = run_push(session, v2)

    assert report.patched == 1 and v2.patch_offer.call_count == 2


def test_validation_error_not_retried(session):
    position, _ = add_listing(session, "a", "10", 1)
    position.last_known_price_usd = Decimal("12")
    session.commit()
    v2 = MagicMock()
    v2.patch_offer.side_effect = GGSellError(422, "invalid")

    report, _ = run_push(session, v2)

    assert v2.patch_offer.call_count == 1 and 1 in report.errors


def test_archived_and_missing_positions_skipped(session):
    p1, _ = add_listing(session, "a", "10", 1, status=ListingStatus.ARCHIVED)
    p2, _ = add_listing(session, "b", "10", 2)
    p1.last_known_price_usd = p2.last_known_price_usd = Decimal("20")
    p2.fz_missing_since = NOW
    session.commit()
    v2 = MagicMock()

    report, _ = run_push(session, v2)

    v2.patch_offer.assert_not_called()
    assert report.unchanged == 1  # архивный вообще не рассматривается


# ----------------------------------------------------------------------
# 4.5a — статусы лотов
# ----------------------------------------------------------------------


def test_fetch_offers_all_pages():
    v2 = MagicMock()
    v2.list_offers.side_effect = [
        {"data": [{"id": 1, "status": "active"}], "pagination": {"has_next_page": True}},
        {"data": [{"id": 2, "status": "paused"}], "pagination": {"has_next_page": False}},
    ]

    offers = fetch_offers(v2, sleep=lambda s: None)

    assert {i: o["status"] for i, o in offers.items()} == {1: "active", 2: "paused"}
    assert [c.kwargs["page"] for c in v2.list_offers.call_args_list] == [1, 2]


def offer(offer_id, status, price, currency="RUB"):
    return {"id": offer_id, "status": status, "price": price, "currency": currency}


def test_sync_listings_statuses(session):
    _, published = add_listing(session, "a", "10", 1)
    _, deleted = add_listing(session, "b", "10", 2, status=ListingStatus.ACTIVE)
    _, same = add_listing(session, "c", "10", 3)

    result = sync_listings(session, {1: offer(1, "active", float(published.price_rub)),
                                     3: offer(3, "draft", float(same.price_rub))})

    assert result == {"status_changed": 1, "archived": 1, "price_changed_on_ggsell": 0}
    assert published.status == "active"
    assert deleted.status == ListingStatus.ARCHIVED
    assert same.status == ListingStatus.DRAFT


def test_manual_price_on_ggsell_restored_by_pricer(session):
    _, listing = add_listing(session, "a", "10", 1)
    expected = listing.price_rub
    sync_listings(session, {1: offer(1, "paused", 10.0)})  # цену поставили руками
    assert listing.price_rub == Decimal("10")
    v2 = MagicMock()

    report, alerts = run_push(session, v2)

    v2.patch_offer.assert_called_once_with(1, {"price": float(expected)})
    assert listing.price_rub == expected
    assert len(report.margin_fixes) == 1 and len(alerts) == 1  # 10 ₽ — ниже себестоимости


# ----------------------------------------------------------------------
# 4.3, 4.5 — доступность у FZ
# ----------------------------------------------------------------------


def test_is_gone_error():
    assert is_gone_error(GONE)
    assert is_gone_error("FazerCards error 404: category not found")
    assert not is_gone_error(TRANSIENT)
    assert not is_gone_error("FazerCards error 429: rate limited")
    assert not is_gone_error(None)


def test_missing_alert_once_then_back(session):
    position, _ = add_listing(session, "a", "10", 1)
    alerts = []

    check_availability(session, [(position, None, GONE)], now=NOW, alert=alerts.append)
    check_availability(session, [(position, None, GONE)], now=NOW, alert=alerts.append)

    assert position.fz_missing_since is not None
    assert len(alerts) == 1 and "Пропало у FazerCards (1)" in alerts[0] and GONE in alerts[0]

    result = check_availability(session, [(position, object(), None)], now=NOW, alert=alerts.append)

    assert position.fz_missing_since is None
    assert result["back"] == 1 and "Снова есть" in alerts[-1]


def test_transient_error_does_not_mark_missing(session):
    position, _ = add_listing(session, "a", "10", 1)
    alerts = []

    check_availability(session, [(position, None, TRANSIENT)], now=NOW, alert=alerts.append)

    assert position.fz_missing_since is None and alerts == []


def test_out_of_stock_alert_once_then_restocked(session):
    position, _ = add_listing(session, "card", "10", 1, source=SourceType.GIFTCARD, stock=0)
    alerts = []

    check_availability(session, [(position, object(), None)], now=NOW, alert=alerts.append)
    check_availability(session, [(position, object(), None)], now=NOW, alert=alerts.append)

    assert position.fz_out_of_stock_since is not None
    assert len(alerts) == 1 and "Закончилось" in alerts[0]

    position.raw_payload = {"stock": 5}
    check_availability(session, [(position, object(), None)], now=NOW, alert=alerts.append)

    assert position.fz_out_of_stock_since is None and "Снова в наличии" in alerts[-1]


def test_positions_without_listing_tracked_but_not_alerted(session):
    unlisted, _ = add_listing(session, "a", "10", 1, listed=False)
    archived, _ = add_listing(session, "b", "10", 2, status=ListingStatus.ARCHIVED)
    alerts = []

    result = check_availability(session, [(unlisted, None, GONE), (archived, None, GONE)],
                                now=NOW, alert=alerts.append)

    assert unlisted.fz_missing_since is not None and archived.fz_missing_since is not None
    assert alerts == [] and result["gone"] == 0


# ----------------------------------------------------------------------
# Выключатель прайсера и сбой GGSell
# ----------------------------------------------------------------------


@pytest.fixture
def fake_v2(monkeypatch):
    v2 = MagicMock()
    v2.__enter__.return_value = v2
    v2.list_offers.return_value = {"data": [{"id": 1, "status": "active"}], "pagination": {"has_next_page": False}}
    monkeypatch.setattr(jobs, "GGSellV2Client", lambda **kw: v2)
    monkeypatch.setenv("GGSELL_API_KEY", "test")
    monkeypatch.setattr(jobs, "load_pricing_config", lambda s: CONFIG)
    return v2


def test_pricer_disabled_syncs_statuses_but_not_prices(session, fake_v2):
    position, listing = add_listing(session, "a", "10", 1)
    position.last_known_price_usd = Decimal("20")
    settings.set_pricer_enabled(session, False)
    session.commit()

    jobs.sync_showcase(session)

    assert listing.status == "active"
    fake_v2.patch_offer.assert_not_called()
    assert settings.get(session, settings.PRICES_UPDATED_AT) is None


def test_pricer_enabled_pushes_and_touches_timestamp(session, fake_v2):
    position, _ = add_listing(session, "a", "10", 1)
    position.last_known_price_usd = Decimal("20")
    settings.set_pricer_enabled(session, True)
    session.commit()

    jobs.sync_showcase(session)

    fake_v2.patch_offer.assert_called_once()
    assert settings.get(session, settings.PRICES_UPDATED_AT) is not None


def test_ggsell_failure_does_not_raise(session, fake_v2, monkeypatch):
    alerts = []
    monkeypatch.setattr(jobs, "notify_admin", alerts.append)
    fake_v2.list_offers.side_effect = GGSellError(401, "unauthorized")

    jobs.sync_showcase(session)  # не падает — цены FZ уже сохранены

    assert len(alerts) == 1 and "упала" in alerts[0]
