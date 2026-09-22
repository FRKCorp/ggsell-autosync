"""Прайсер: пересчёт цены поставщика (USD) в цену на GGSell (RUB) с
наценкой, плюс защита от продажи в убыток (см. architecture-notes.md,
сценарии "FZ не отвечает" и "цена скакнула между синхронизациями").

Все функции чистые (без побочных эффектов, без обращений к БД/API) —
специально, чтобы легко покрывались тестами и не зависели от контекста
вызова. Интеграция с БД/GGSell (пересчёт Listing.price_rub, реальный
PATCH оффера) — отдельный модуль поверх этого, ещё не написан.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from dotenv import load_dotenv


@dataclass(frozen=True)
class PricingConfig:
    exchange_rate_usd_to_rub: Decimal
    markup_percent: Decimal
    min_margin_percent: Decimal
    max_price_deviation_percent: Decimal

    @classmethod
    def from_env(cls) -> "PricingConfig":
        load_dotenv()
        return cls(
            exchange_rate_usd_to_rub=Decimal(os.getenv("EXCHANGE_RATE_USD_TO_RUB", "95.0")),
            markup_percent=Decimal(os.getenv("MARKUP_PERCENT", "15.0")),
            min_margin_percent=Decimal(os.getenv("MIN_MARGIN_PERCENT", "3.0")),
            max_price_deviation_percent=Decimal(os.getenv("MAX_PRICE_DEVIATION_PERCENT", "5.0")),
        )


def base_cost_rub(price_usd: Decimal, exchange_rate: Decimal) -> Decimal:
    """Себестоимость в рублях по курсу, без наценки — точка отсчёта для
    расчёта маржи."""
    return price_usd * exchange_rate


def calculate_price_rub(
    price_usd: Decimal,
    config: PricingConfig,
    *,
    round_to: Decimal = Decimal("0.01"),
) -> Decimal:
    """Цена на GGSell = себестоимость * (1 + наценка%).

    Округление ВВЕРХ (ROUND_CEILING), а не по банковским/школьным правилам —
    округление вниз тихо съедало бы часть наценки на каждой отдельной
    позиции, а таких позиций сотни.
    """
    cost = base_cost_rub(price_usd, config.exchange_rate_usd_to_rub)
    price = cost * (Decimal(1) + config.markup_percent / Decimal(100))
    return price.quantize(round_to, rounding=ROUND_CEILING)


def actual_margin_percent(
    price_rub: Decimal, price_usd: Decimal, exchange_rate: Decimal
) -> Decimal:
    """Реальная маржа в процентах для уже посчитанной/выставленной цены —
    например, чтобы проверить, не просела ли она ниже допустимого порога
    из-за изменения курса поставщика с момента последнего PATCH.
    """
    cost = base_cost_rub(price_usd, exchange_rate)
    if cost == 0:
        raise ValueError("price_usd не может быть равен 0 — деление на ноль при расчёте маржи")
    return (price_rub - cost) / cost * Decimal(100)


def is_margin_safe(
    price_rub: Decimal, price_usd: Decimal, exchange_rate: Decimal, config: PricingConfig
) -> bool:
    """True, если текущая цена всё ещё даёт маржу не ниже MIN_MARGIN_PERCENT."""
    return actual_margin_percent(price_rub, price_usd, exchange_rate) >= config.min_margin_percent


@dataclass(frozen=True)
class PriceDeviationCheck:
    cached_price_usd: Decimal
    live_price_usd: Decimal
    deviation_percent: Decimal
    within_threshold: bool


def check_price_deviation(
    cached_price_usd: Decimal, live_price_usd: Decimal, config: PricingConfig
) -> PriceDeviationCheck:
    """Сверка цены, по которой продали (cached, из Position на момент
    продажи), с реальной ценой поставщика прямо перед списанием (live,
    свежий вызов API) — та самая "проверка цены в момент заказа", которую
    обсуждали как защиту от 12-часового окна между синхронизациями.

    Если deviation_percent > MAX_PRICE_DEVIATION_PERCENT — заказ не
    отправляется автоматически, Order уходит в PRICE_REJECTED -> ручной
    режим, а не списывается вслепую.
    """
    if cached_price_usd == 0:
        raise ValueError("cached_price_usd не может быть равен 0")
    deviation = abs(live_price_usd - cached_price_usd) / cached_price_usd * Decimal(100)
    return PriceDeviationCheck(
        cached_price_usd=cached_price_usd,
        live_price_usd=live_price_usd,
        deviation_percent=deviation,
        within_threshold=deviation <= config.max_price_deviation_percent,
    )
