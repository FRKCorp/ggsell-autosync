from decimal import Decimal

import pytest

from app.models.position import Position, SourceType
from app.offers.builder import build_group_offer, build_offer, game_name, nominal_title
from app.pricing.calculator import PricingConfig

CONFIG = PricingConfig(
    exchange_rate_usd_to_rub=Decimal("89.15"),
    markup_percent=Decimal("15"),
    min_margin_percent=Decimal("3"),
    max_price_deviation_percent=Decimal("5"),
)
APPLE_RU = {"category_id": 110397, "fee": 0.06, "payment_fee": 0.027}


def card(region, nominal, currency, price):
    return Position(
        external_id=f"giftcard:app_store_itunes_{region.lower()}:{nominal}_{currency.lower()}",
        source_type=SourceType.GIFTCARD, fz_category_id=f"app_store_itunes_{region.lower()}",
        fz_offer_id=f"{nominal}_{currency.lower()}", region=region,
        name=f"App Store & iTunes ({region}) — {nominal} {currency}",
        raw_payload={"name": f"{nominal} {currency}"}, last_known_price_usd=Decimal(price),
    )


@pytest.fixture
def russia():
    return [card("RU", 500, "RUB", "7.3764"), card("RU", 1000, "RUB", "14.7528"), card("RU", 7000, "RUB", "103.82")]


def build(members):
    return build_group_offer(members, APPLE_RU, CONFIG, option_title_ru="Номинал", option_title_en="Denomination",
                             webhook_url="https://example.ru:8443/webhooks/ggsell")


def test_group_title_has_no_nominal_and_no_ampersand(russia):
    draft = build(russia)

    assert draft.payload["title_ru"] == "App Store / iTunes RU* Подарочная карта (RUB) | Код | Автодоставка"
    assert draft.payload["title_en"] == "App Store / iTunes RU* Gift card (RUB) | Code | Auto delivery"
    assert all("&" not in draft.payload[k] for k in ("title_ru", "title_en", "description_ru", "instructions_ru"))
    assert len(draft.payload["title_ru"]) <= 100


def test_group_texts_list_nominals_and_option(russia):
    draft = build(russia)

    assert "Выберите номинал при оформлении заказа: 500, 1000, 7000 RUB." in draft.payload["description_ru"]
    assert "Choose the denomination when placing the order: 500, 1000, 7000 RUB." in draft.payload["description_en"]
    assert "параметре «Номинал»" in draft.payload["instructions_ru"]
    assert "Активируйте его в App Store / iTunes" in draft.payload["instructions_ru"]
    assert "{" not in draft.payload["description_ru"] + draft.payload["instructions_en"]


def test_group_option_and_variants(russia):
    draft = build(russia)

    assert draft.option == {"type": "radio_button", "status": "active", "title_ru": "Номинал",
                            "title_en": "Denomination", "is_required": True, "position": 0}
    assert [v.title_ru for v in draft.variants] == ["500 RUB", "1000 RUB", "7000 RUB"]
    assert draft.base.position is russia[0]
    assert draft.payload["price"] == float(draft.base.price_rub)
    assert draft.variants[0].price_rub < draft.variants[1].price_rub < draft.variants[2].price_rub
    assert draft.payload["category_id"] == 110397 and draft.payload["max_quantity"] == 1
    assert draft.payload["notification_settings"]["url"].endswith("/webhooks/ggsell")


def test_variant_price_equals_single_lot_price(russia):
    """Цена номинала в группе — та же, что была бы у отдельного лота."""
    draft = build(russia)

    single = build_offer(russia[1], APPLE_RU, CONFIG, webhook_url=None)
    assert draft.variants[1].price_rub == single.price_rub


def test_mixed_currencies_fall_back_to_plain_list():
    members = [card("EU", 5, "EUR", "5.6"), Position(
        external_id="giftcard:app_store_itunes_eu:x", source_type=SourceType.GIFTCARD,
        fz_category_id="app_store_itunes_eu", fz_offer_id="x", region="EU",
        name="App Store & iTunes (EU) — 10 USD", raw_payload={"name": "10 USD"}, last_known_price_usd=Decimal("10"))]

    draft = build(members)

    assert draft.payload["title_ru"] == "App Store / iTunes EU* Подарочная карта | Код | Автодоставка"
    assert "5 EUR, 10 USD" in draft.payload["description_ru"]


def test_ampersand_fixed_in_single_lots_too(russia):
    assert game_name(russia[0]) == "App Store / iTunes"
    assert "&" not in build_offer(russia[0], APPLE_RU, CONFIG, webhook_url=None).payload["title_ru"]
    assert nominal_title(russia[0]) == "500 RUB"


def test_empty_group_rejected():
    with pytest.raises(ValueError):
        build([])
