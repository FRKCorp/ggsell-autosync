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


# ----------------------------------------------------------------------
# Комиссия GGSell (roadmap 3.4a)
# ----------------------------------------------------------------------

from app.pricing.calculator import NO_FEES, GGSellFees, net_payout_rub  # noqa: E402

# Категория валюты: fee 2% + payment_fee 2.7% (как у тестовой продажи 22.09).
CURRENCY_FEES = GGSellFees(fee=Decimal("0.02"), payment_fee=Decimal("0.027"))


def test_net_payout_matches_real_sale():
    """Реальная продажа 22.09 (заказ 51308662): лот 1.00 ₽, категория с
    fee 2% + 2.7% — GGSell выплатил продавцу 0.95 ₽ (profit)."""
    payout = net_payout_rub(Decimal("1.00"), CURRENCY_FEES)
    assert payout == Decimal("0.953")
    assert payout.quantize(Decimal("0.01")) == Decimal("0.95")


def test_price_with_fees_keeps_full_markup_after_payout(config):
    price_usd = Decimal("10.00")
    price_rub = calculate_price_rub(price_usd, config, CURRENCY_FEES)

    # 10 * 95 * 1.15 / (1 − 0.047) = 1146.3798…, округление вверх
    assert price_rub == Decimal("1146.38")
    cost = base_cost_rub(price_usd, config.exchange_rate_usd_to_rub)
    assert net_payout_rub(price_rub, CURRENCY_FEES) >= cost * Decimal("1.15")


def test_price_without_fees_unchanged(config):
    """Вызовы без комиссий (старое поведение) не изменились."""
    assert calculate_price_rub(Decimal("1.00"), config) == Decimal("109.25")
    assert calculate_price_rub(Decimal("1.00"), config, NO_FEES) == Decimal("109.25")


def test_old_price_is_loss_making_in_high_fee_category(config):
    """Ради чего всё это: цена без учёта комиссии в категории с 15% + 2.7%
    не проходит даже минимальную маржу 3%."""
    fees = GGSellFees(fee=Decimal("0.15"), payment_fee=Decimal("0.027"))
    price_usd = Decimal("10.00")
    old_price = calculate_price_rub(price_usd, config)  # без комиссии
    assert not is_margin_safe(old_price, price_usd, config.exchange_rate_usd_to_rub, config, fees)
    new_price = calculate_price_rub(price_usd, config, fees)
    assert is_margin_safe(new_price, price_usd, config.exchange_rate_usd_to_rub, config, fees)


def test_margin_is_computed_from_payout(config):
    price_usd = Decimal("2.60")
    price_rub = calculate_price_rub(price_usd, config, CURRENCY_FEES)
    margin = actual_margin_percent(price_rub, price_usd, config.exchange_rate_usd_to_rub, CURRENCY_FEES)
    assert config.markup_percent <= margin < config.markup_percent + Decimal("0.1")


def test_fees_from_category_floats():
    fees = GGSellFees.from_category(0.07, 0.027)
    assert fees.fee == Decimal("0.07") and fees.payment_fee == Decimal("0.027")
    assert GGSellFees.from_category(0.02, None).total == Decimal("0.02")


@pytest.mark.parametrize("fee, payment_fee", [("-0.01", "0"), ("0.9", "0.1"), ("1", "0")])
def test_invalid_fees_rejected(fee, payment_fee):
    with pytest.raises(ValueError):
        GGSellFees(fee=Decimal(fee), payment_fee=Decimal(payment_fee))


def test_all_mapped_categories_give_full_markup(config):
    """Для каждой из подобранных категорий (реальные fee из
    data/ggsell_category_map.json) выплата продавцу покрывает наценку."""
    import json
    from pathlib import Path

    mapping = json.loads(
        (Path(__file__).resolve().parents[2] / "data" / "ggsell_category_map.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(mapping) > 2000
    price_usd = Decimal("3.33")
    cost = base_cost_rub(price_usd, config.exchange_rate_usd_to_rub)
    for external_id, category in mapping.items():
        fees = GGSellFees.from_category(category["fee"], category["payment_fee"])
        price_rub = calculate_price_rub(price_usd, config, fees)
        assert net_payout_rub(price_rub, fees) >= cost * Decimal("1.15"), external_id
