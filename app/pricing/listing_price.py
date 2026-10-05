"""Цена лота на GGSell из текущих настроек (roadmap 3.4, 3.4b).

Связывает чистый калькулятор (app/pricing/calculator.py) с тем, что живёт в
БД: курс (ЦБ + надбавка) и глобальная наценка — в настройках (app/settings.py,
меняются из Telegram-панели), индивидуальная наценка и комиссии GGSell — у
лота (Listing.markup_percent, ggsell_fee, ggsell_payment_fee).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app import settings
from app.models.listing import Listing
from app.models.position import is_unit_priced
from app.pricing.calculator import GGSellFees, PricingConfig, calculate_price_rub


def load_pricing_config(session: Session) -> PricingConfig:
    """Курс и глобальная наценка — из настроек; пороги защиты (5% / 3%,
    подтверждены клиентом) — из .env, как и раньше."""
    env = PricingConfig.from_env()
    return replace(
        env,
        exchange_rate_usd_to_rub=settings.get_decimal(session, settings.USD_RUB_RATE),
        markup_percent=settings.global_markup_percent(session),
    )


def effective_markup(config: PricingConfig, listing_markup: Optional[Decimal]) -> Decimal:
    """Индивидуальная наценка лота, если задана, иначе глобальная."""
    return listing_markup if listing_markup is not None else config.markup_percent


UNIT_PRICE_ROUND = Decimal("0.01")


def price_rub(
    price_usd: Decimal,
    config: PricingConfig,
    fees: GGSellFees,
    listing_markup: Optional[Decimal] = None,
    *,
    unit_priced: bool = False,
) -> Decimal:
    """Цена на витрине: курс + наценка лота (или глобальная) + комиссии
    категории, вверх до целого рубля. У лотов с ценой за единицу (1 ₽ Steam,
    1 звезда — калькулятор GGSell) — вверх до копейки: до рубля 1.33 ₽ стали
    бы 2 ₽, +50% к цене (notes 6.22)."""
    lot_config = replace(config, markup_percent=effective_markup(config, listing_markup))
    if unit_priced:
        return calculate_price_rub(price_usd, lot_config, fees, round_to=UNIT_PRICE_ROUND)
    return calculate_price_rub(price_usd, lot_config, fees)


def listing_price_rub(listing: Listing, config: PricingConfig) -> Decimal:
    """Цена существующего лота по актуальной цене поставщика его позиции."""
    return price_rub(
        listing.position.last_known_price_usd,
        config,
        GGSellFees(fee=listing.ggsell_fee, payment_fee=listing.ggsell_payment_fee),
        listing.markup_percent,
        unit_priced=is_unit_priced(listing.position.source_type),
    )


def set_listing_markup(listing: Listing, percent: Optional[Decimal]) -> None:
    """Индивидуальная наценка лота (None — вернуть на глобальную). Новая цена
    уходит на витрину с ближайшей синхронизацией цен (этап 4)."""
    if percent is not None and (percent < 0 or percent > 1000):
        raise ValueError(f"Наценка должна быть от 0 до 1000%, получено {percent}")
    listing.markup_percent = percent
