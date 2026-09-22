from decimal import Decimal

import pytest

from app.pricing.calculator import (
    PricingConfig,
    actual_margin_percent,
    base_cost_rub,
    calculate_price_rub,
    check_price_deviation,
    is_margin_safe,
)


@pytest.fixture
def config() -> PricingConfig:
    return PricingConfig(
        exchange_rate_usd_to_rub=Decimal("95.0"),
        markup_percent=Decimal("15.0"),
        min_margin_percent=Decimal("3.0"),
        max_price_deviation_percent=Decimal("5.0"),
    )


def test_base_cost_rub():
    assert base_cost_rub(Decimal("1.00"), Decimal("95.0")) == Decimal("95.00")


def test_calculate_price_rub_applies_markup(config):
    # 1 USD * 95 RUB * 1.15 = 109.25 RUB — ровное число, округление вверх
    # не должно ничего изменить.
    price = calculate_price_rub(Decimal("1.00"), config)
    assert price == Decimal("109.25")


def test_calculate_price_rub_rounds_up_not_down(config):
    # Подбираем цену так, чтобы после наценки получилось дробное значение
    # с большим количеством знаков — например 0.7450 USD:
    # 0.7450 * 95 * 1.15 = 81.394625 -> округление вверх должно дать 81.40,
    # а не 81.39 (что дало бы школьное round-half-up).
    price = calculate_price_rub(Decimal("0.7450"), config)
    assert price == Decimal("81.40")


def test_actual_margin_percent_matches_configured_markup(config):
    price_usd = Decimal("2.60")
    price_rub = calculate_price_rub(price_usd, config)
    margin = actual_margin_percent(price_rub, price_usd, config.exchange_rate_usd_to_rub)
    # Маржа должна быть >= настроенной наценки (округление вверх её слегка
    # увеличивает, поэтому строго ">=", а не "==").
    assert margin >= config.markup_percent


def test_actual_margin_percent_zero_cost_raises():
    with pytest.raises(ValueError):
        actual_margin_percent(Decimal("100"), Decimal("0"), Decimal("95.0"))


def test_is_margin_safe_true_when_price_has_enough_markup(config):
    price_usd = Decimal("10.00")
    price_rub = calculate_price_rub(price_usd, config)
    assert is_margin_safe(price_rub, price_usd, config.exchange_rate_usd_to_rub, config) is True


def test_is_margin_safe_false_when_price_too_low(config):
    price_usd = Decimal("10.00")
    # Цена без наценки вообще — маржа 0%, ниже настроенного порога 3%.
    price_rub = base_cost_rub(price_usd, config.exchange_rate_usd_to_rub)
    assert is_margin_safe(price_rub, price_usd, config.exchange_rate_usd_to_rub, config) is False


def test_check_price_deviation_within_threshold(config):
    result = check_price_deviation(Decimal("10.00"), Decimal("10.30"), config)
    assert result.within_threshold is True
    assert result.deviation_percent == Decimal("3.00")


def test_check_price_deviation_exceeds_threshold(config):
    result = check_price_deviation(Decimal("10.00"), Decimal("11.00"), config)
    assert result.within_threshold is False
    assert result.deviation_percent == Decimal("10.00")


def test_check_price_deviation_zero_cached_raises(config):
    with pytest.raises(ValueError):
        check_price_deviation(Decimal("0"), Decimal("5.00"), config)


def test_check_price_deviation_handles_price_drop_too(config):
    # Расхождение считается по модулю — падение цены поставщика тоже
    # должно засчитываться как отклонение, не только рост.
    result = check_price_deviation(Decimal("10.00"), Decimal("9.00"), config)
    assert result.deviation_percent == Decimal("10.00")
    assert result.within_threshold is False
