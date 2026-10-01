from decimal import Decimal
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app import settings
from app.models.base import Base
from app.models.listing import Listing
from app.models.position import Position, SourceType
from app.pricing.exchange_rate import (
    CBR_JSON_MIRROR_URL,
    CBR_XML_URL,
    ExchangeRateError,
    apply_premium,
    current_rate,
    fetch_cbr_usd_rub,
    parse_cbr_xml,
    refresh_rate,
    set_premium,
)
from app.pricing.listing_price import (
    listing_price_rub,
    load_pricing_config,
    price_rub,
    set_listing_markup,
)
from app.pricing.calculator import GGSellFees

# Кусок реального ответа cbr.ru 01.10 (windows-1251, запятая в числе).
CBR_XML = (
    '<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="01.10.2026" name="Foreign Currency Market">'
    '<Valute ID="R01010"><NumCode>036</NumCode><CharCode>AUD</CharCode><Nominal>1</Nominal>'
    "<Name>Австралийский доллар</Name><Value>58,2990</Value></Valute>"
    '<Valute ID="R01235"><NumCode>840</NumCode><CharCode>USD</CharCode><Nominal>1</Nominal>'
    "<Name>Доллар США</Name><Value>83,5588</Value></Valute></ValCurs>"
).encode("cp1251")


@pytest.fixture
def session(monkeypatch):
    monkeypatch.setenv("MARKUP_PERCENT", "15.0")
    monkeypatch.setenv("EXCHANGE_RATE_PREMIUM_PERCENT", "5.0")
    monkeypatch.setenv("EXCHANGE_RATE_USD_TO_RUB", "95.0")
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def response(url, content=None, json_data=None, status=200):
    if json_data is not None:
        return httpx.Response(status, json=json_data, request=httpx.Request("GET", url))
    return httpx.Response(status, content=content or b"", request=httpx.Request("GET", url))


# ----------------------------------------------------------------------
# Курс ЦБ
# ----------------------------------------------------------------------


def test_parse_cbr_xml_real_format():
    assert parse_cbr_xml(CBR_XML) == Decimal("83.5588")


def test_parse_cbr_xml_respects_nominal():
    xml = '<ValCurs><Valute><CharCode>USD</CharCode><Nominal>10</Nominal><Value>835,588</Value></Valute></ValCurs>'
    assert parse_cbr_xml(xml.encode()) == Decimal("83.5588")


def test_fetch_falls_back_to_mirror():
    def get(url):
        if url == CBR_XML_URL:
            return response(url, status=503)
        return response(url, json_data={"Valute": {"USD": {"Value": 83.5588, "Nominal": 1}}})

    assert fetch_cbr_usd_rub(get) == Decimal("83.5588")


def test_fetch_both_sources_down_raises():
    with pytest.raises(ExchangeRateError) as error:
        fetch_cbr_usd_rub(lambda url: response(url, status=500))
    assert "cbr.ru:" in str(error.value) and "cbr-xml-daily:" in str(error.value)


def test_apply_premium():
    # 83.5588 * 1.05 = 87.73674 → 87.7367
    assert apply_premium(Decimal("83.5588"), Decimal("5")) == Decimal("87.7367")
    assert apply_premium(Decimal("83.5588"), Decimal("0")) == Decimal("83.5588")


def test_refresh_rate_saves_cbr_plus_premium(session):
    info = refresh_rate(session, fetch=lambda: Decimal("83.5588"))

    assert info.fresh and info.rate == Decimal("87.7367") and info.cbr_rate == Decimal("83.5588")
    stored = current_rate(session)
    assert stored.rate == Decimal("87.7367")
    assert stored.premium_percent == Decimal("5.0")
    assert stored.updated_at is not None


def test_refresh_rate_keeps_last_rate_when_cbr_down(session):
    refresh_rate(session, fetch=lambda: Decimal("83.5588"))

    def down():
        raise ExchangeRateError("cbr.ru: timeout")

    info = refresh_rate(session, fetch=down)
    assert not info.fresh
    assert info.rate == Decimal("87.7367")  # прежний, не сброшен


def test_rate_before_first_fetch_is_env_fallback(session):
    assert current_rate(session).rate == Decimal("95.0")


def test_set_premium_recomputes_rate_without_cbr_call(session):
    refresh_rate(session, fetch=lambda: Decimal("80"))
    info = set_premium(session, Decimal("10"))
    assert info.rate == Decimal("88.0000")
    assert info.premium_percent == Decimal("10")
    with pytest.raises(ValueError):
        set_premium(session, Decimal("-1"))


# ----------------------------------------------------------------------
# Настройки и наценка
# ----------------------------------------------------------------------


def test_global_markup_default_from_env_then_from_db(session):
    assert settings.global_markup_percent(session) == Decimal("15.0")
    settings.set_global_markup_percent(session, Decimal("8"))
    assert settings.global_markup_percent(session) == Decimal("8")
    with pytest.raises(ValueError):
        settings.set_global_markup_percent(session, Decimal("-5"))


def test_load_pricing_config_uses_settings(session):
    refresh_rate(session, fetch=lambda: Decimal("80"))  # → 84 ₽ при надбавке 5%
    settings.set_global_markup_percent(session, Decimal("8"))

    config = load_pricing_config(session)

    assert config.exchange_rate_usd_to_rub == Decimal("84.0000")
    assert config.markup_percent == Decimal("8")


def test_listing_markup_overrides_global(session):
    refresh_rate(session, fetch=lambda: Decimal("80"))  # 84 ₽
    settings.set_global_markup_percent(session, Decimal("10"))
    config = load_pricing_config(session)
    fees = GGSellFees()

    # 1 $ * 84 * 1.10 = 92.4 → 93; с индивидуальной 20%: 100.8 → 101
    assert price_rub(Decimal("1"), config, fees) == Decimal("93")
    assert price_rub(Decimal("1"), config, fees, listing_markup=Decimal("20")) == Decimal("101")
    assert price_rub(Decimal("1"), config, fees, listing_markup=Decimal("0")) == Decimal("84")


def test_listing_price_uses_listing_fees_and_markup(session):
    refresh_rate(session, fetch=lambda: Decimal("80"))  # 84 ₽
    settings.set_global_markup_percent(session, Decimal("10"))
    position = Position(
        external_id="topup:x:y", source_type=SourceType.TOPUP, name="X",
        last_known_price_usd=Decimal("10"), raw_payload={},
    )
    listing = Listing(
        position=position, ggsell_offer_id=1, price_rub=Decimal("0"),
        price_source_usd_at_sync=Decimal("10"),
        ggsell_fee=Decimal("0.02"), ggsell_payment_fee=Decimal("0.027"),
    )
    session.add_all([position, listing])
    session.flush()
    config = load_pricing_config(session)

    # 10 * 84 * 1.10 / (1 − 0.047) = 969.56… → 970
    assert listing_price_rub(listing, config) == Decimal("970")
    set_listing_markup(listing, Decimal("20"))
    # 10 * 84 * 1.20 / 0.953 = 1057.71… → 1058
    assert listing_price_rub(listing, config) == Decimal("1058")
    set_listing_markup(listing, None)
    assert listing_price_rub(listing, config) == Decimal("970")
