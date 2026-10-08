from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsError
from app.clients.ggsell import GGSellError
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.position import Position, SourceType
from app.offers.uploader import FieldsSource, upload_positions
from app.pricing.calculator import PricingConfig

CONFIG = PricingConfig(Decimal("87.7367"), Decimal("15"), Decimal("3"), Decimal("5"))
MLBB_FIELDS = [
    {"key": "player_id", "label": "Player ID", "type": "text"},
    {"key": "server_id", "label": "Server ID", "type": "text"},
]
CATEGORY_MAP = {
    "topup:mlbb_ru:275": {"category_id": 100319013, "fee": 0.04, "payment_fee": 0.027},
    "giftcard:google_play_us:25_usd": {"category_id": 115011, "fee": 0.04, "payment_fee": 0.027},
}


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        s.add_all([
            Position(external_id="topup:mlbb_ru:275", source_type=SourceType.TOPUP, fz_category_id="mlbb_ru",
                     name="Mobile Legends (RU) — 275 Diamonds", region="RU",
                     last_known_price_usd=Decimal("4.6857"), raw_payload={"name": "275 Diamonds"}),
            Position(external_id="giftcard:google_play_us:25_usd", source_type=SourceType.GIFTCARD,
                     fz_category_id="google_play_us", name="Google Play (US) — 25 USD", region="US",
                     last_known_price_usd=Decimal("24.58"), raw_payload={"name": "25 USD"}),
            Position(external_id="giftcard:razer_gold_us:10_usd", source_type=SourceType.GIFTCARD,
                     fz_category_id="razer_gold_us", name="Razer Gold (US) — 10 USD", region="US",
                     last_known_price_usd=Decimal("10"), raw_payload={"name": "10 USD"}),
        ])
        s.commit()
        yield s


def positions(session):
    return session.scalars(select(Position).order_by(Position.id)).all()


def listings(session):
    return session.scalars(select(Listing).order_by(Listing.id)).all()


def make_v2():
    v2 = MagicMock()
    ids = iter(range(5001, 6000))
    v2.create_offer.side_effect = lambda payload: {"data": {"id": next(ids), "status": "draft"}}
    v2.list_offer_options.return_value = []
    return v2


def fields_source(fields=MLBB_FIELDS):
    fz = MagicMock()
    fz.get_topup_offers.return_value = {"fields": fields, "offers": []}
    return FieldsSource(fz, fallback={})


def run(session, v2, **kwargs):
    kwargs.setdefault("fields", fields_source())
    fields = kwargs.pop("fields")
    return upload_positions(session, v2, positions(session), CATEGORY_MAP, CONFIG, fields,
                            webhook_url="https://example.test/webhooks/ggsell", pause_seconds=0,
                            sleep=lambda s: None, **kwargs)


def test_creates_drafts_records_listings_and_options(session):
    v2 = make_v2()

    report = run(session, v2)

    assert report.created == ["topup:mlbb_ru:275", "giftcard:google_play_us:25_usd"]
    assert report.skipped_no_category == 1  # Razer Gold — нет категории (2.13)
    assert v2.create_offer.call_count == 2
    payload = v2.create_offer.call_args_list[0].args[0]
    assert payload["title_ru"].startswith("Mobile Legends RU* Алмазы 275")
    assert payload["notification_settings"]["url"] == "https://example.test/webhooks/ggsell"

    topup_listing, card_listing = listings(session)
    assert topup_listing.ggsell_offer_id == 5001
    assert topup_listing.status == ListingStatus.DRAFT
    assert topup_listing.price_rub == Decimal("507")
    assert topup_listing.price_source_usd_at_sync == Decimal("4.6857")
    assert topup_listing.ggsell_category_id == 100319013
    assert topup_listing.ggsell_fee == Decimal("0.04") and topup_listing.ggsell_payment_fee == Decimal("0.027")
    assert topup_listing.options_attached is True
    assert card_listing.options_attached is True  # у карт опций нет
    # Опции — только у топапа
    v2.create_or_update_options.assert_called_once()
    assert v2.create_or_update_options.call_args.args[0] == 5001


def test_rerun_creates_nothing(session):
    v2 = make_v2()
    run(session, v2)

    report = run(session, v2)

    assert report.created == [] and report.skipped_existing == 2
    assert v2.create_offer.call_count == 2  # только из первого запуска
    assert session.scalar(select(func.count()).select_from(Listing)) == 2


def test_options_failure_keeps_listing_and_rerun_fixes_only_options(session):
    v2 = make_v2()
    v2.create_or_update_options.side_effect = GGSellError(422, {"errors": "boom"})

    report = run(session, v2)

    assert "topup:mlbb_ru:275" in report.created
    assert "повторный запуск досоздаст" in report.errors["topup:mlbb_ru:275"]
    topup_listing = listings(session)[0]
    assert topup_listing.options_attached is False

    v2.create_or_update_options.side_effect = None
    report = run(session, v2)

    assert report.options_fixed == ["topup:mlbb_ru:275"]
    assert report.created == [] and not report.errors
    assert v2.create_offer.call_count == 2  # оффер не пересоздавали
    session.refresh(topup_listing)
    assert topup_listing.options_attached is True


def test_create_error_does_not_stop_other_positions(session):
    v2 = make_v2()
    v2.create_offer.side_effect = [GGSellError(422, {"errors": "bad title"}), {"data": {"id": 7001}}]

    report = run(session, v2)

    assert set(report.errors) == {"topup:mlbb_ru:275"}
    assert report.created == ["giftcard:google_play_us:25_usd"]
    assert [l.ggsell_offer_id for l in listings(session)] == [7001]


def test_retries_on_gateway_timeout(session):
    v2 = make_v2()
    ok = v2.create_offer.side_effect
    calls = {"n": 0}

    def flaky(payload):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GGSellError(504, "Gateway Time-out")
        return ok(payload)

    v2.create_offer.side_effect = flaky
    report = run(session, v2)

    assert not report.errors and len(report.created) == 2


def test_limit_counts_only_new_lots(session):
    v2 = make_v2()
    report = run(session, v2, limit=1)
    assert report.created == ["topup:mlbb_ru:275"]
    report = run(session, v2, limit=1)
    assert report.created == ["giftcard:google_play_us:25_usd"] and report.skipped_existing == 1


def test_dry_run_touches_nothing(session):
    v2 = make_v2()
    report = run(session, v2, dry_run=True)
    assert len(report.created) == 2
    v2.create_offer.assert_not_called()
    assert listings(session) == []


def test_fields_fall_back_to_file_when_fz_down():
    fz = MagicMock()
    fz.get_topup_offers.side_effect = FazerCardsError(503, {"error": "down"})
    source = FieldsSource(fz, fallback={"mlbb_ru": MLBB_FIELDS})
    assert source.get("mlbb_ru") == MLBB_FIELDS
    source.get("mlbb_ru")
    assert fz.get_topup_offers.call_count == 1  # кэш на категорию


@pytest.mark.parametrize("domain, port, expected", [
    ("shop.example.ru", "443", "https://shop.example.ru/webhooks/ggsell"),
    ("shop.example.ru, www.shop.example.ru", "", "https://shop.example.ru/webhooks/ggsell"),
    ("shop.example.ru", "8443", "https://shop.example.ru:8443/webhooks/ggsell"),
    ("", "443", None),
])
def test_default_webhook_url(monkeypatch, domain, port, expected):
    from app.offers.uploader import default_webhook_url

    monkeypatch.delenv("GGSELL_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("DOMAIN", domain)
    monkeypatch.setenv("HTTPS_PORT", port)
    assert default_webhook_url() == expected
