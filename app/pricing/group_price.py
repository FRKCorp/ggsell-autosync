"""Цены группового лота: базовая цена оффера и надбавки вариантов «Номинал».

Цена каждого номинала считается так же, как у отдельного лота
(listing_price.price_rub). На GGSell у оффера — цена базового номинала,
у варианта — разница с ней: надбавка (increase) или скидка (decrease), если
номинал вдруг стал дешевле базового.

Как у отдельных лотов: номинал переоценивается, если его цена ушла больше
порога или текущая цена уже не даёт минимальную маржу. Если меняется цена
базового номинала, пересчитываются надбавки всех вариантов — иначе итоговые
цены на витрине съедут.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Optional

from app.models.listing import Listing
from app.models.listing_variant import ListingVariant
from app.pricing.calculator import GGSellFees, PricingConfig, is_margin_safe
from app.pricing.listing_price import price_rub

ACTIVE = "active"


def variant_payload(variant_id: Optional[int], price: Decimal, base_price: Decimal) -> dict[str, Any]:
    """Тело варианта для create_or_update_variants: надбавка к базовой цене."""
    delta = price - base_price
    payload: dict[str, Any] = {
        "price": float(abs(delta)),
        "discount_type": "fixed",
        "impact_type": "increase" if delta >= 0 else "decrease",
    }
    if variant_id is not None:
        payload["id"] = variant_id
    return payload


def target_price(listing: Listing, variant: ListingVariant, config: PricingConfig) -> Decimal:
    """Расчётная цена номинала по актуальной цене поставщика."""
    fees = GGSellFees(fee=listing.ggsell_fee, payment_fee=listing.ggsell_payment_fee)
    return price_rub(variant.position.last_known_price_usd, config, fees, listing.markup_percent)


def _sellable(variant: ListingVariant) -> bool:
    position = variant.position
    return (variant.status == ACTIVE and position.fz_missing_since is None
            and bool(position.last_known_price_usd) and position.last_known_price_usd > 0)


@dataclass
class GroupPriceUpdate:
    base_price: Decimal  # цена оффера после обновления
    base_changed: bool
    prices: dict[int, Decimal] = field(default_factory=dict)  # id ListingVariant → новая полная цена
    variants: list[dict[str, Any]] = field(default_factory=list)  # тела для create_or_update_variants
    margin_fixes: list[str] = field(default_factory=list)  # номиналы, где цена не давала мин. маржу

    @property
    def empty(self) -> bool:
        return not self.base_changed and not self.variants


def plan_group_update(listing: Listing, config: PricingConfig, threshold_percent: Decimal) -> GroupPriceUpdate:
    """Что отправить на GGSell для группового лота. Номиналы, которых нет у FZ,
    и скрытые варианты не трогаем — как отдельные лоты при пропаже у FZ."""
    fees = GGSellFees(fee=listing.ggsell_fee, payment_fee=listing.ggsell_payment_fee)
    variants = sorted(listing.variants, key=lambda v: v.sort_order)
    new_prices: dict[int, Decimal] = {}
    margin_fixes: list[str] = []
    for variant in variants:
        if not _sellable(variant):
            continue
        current = variant.price_rub
        target = target_price(listing, variant, config)
        if target == current:
            continue
        unsafe = not is_margin_safe(current, variant.position.last_known_price_usd,
                                    config.exchange_rate_usd_to_rub, config, fees)
        change = abs(target - current) / current * 100 if current else Decimal(100)
        if unsafe or change >= threshold_percent:
            new_prices[variant.id] = target
            if unsafe:
                margin_fixes.append(f"{variant.position.name}: {current} → {target} ₽")

    base = next((v for v in variants if v.position_id == listing.position_id), variants[0] if variants else None)
    base_price = new_prices.get(base.id, listing.price_rub) if base is not None else listing.price_rub
    update = GroupPriceUpdate(base_price=base_price, base_changed=base_price != listing.price_rub,
                              prices=new_prices, margin_fixes=margin_fixes)
    for variant in variants:
        if variant.ggsell_variant_id is None or variant.status != ACTIVE:
            continue
        price = new_prices.get(variant.id, variant.price_rub)
        old_delta = variant.price_rub - listing.price_rub
        if price - base_price != old_delta:
            update.variants.append(variant_payload(variant.ggsell_variant_id, price, base_price))
    return update


def apply_group_update(listing: Listing, update: GroupPriceUpdate) -> None:
    """Записать в БД то, что ушло на GGSell."""
    listing.price_rub = update.base_price
    for variant in listing.variants:
        if variant.id not in update.prices:
            continue
        variant.price_rub = update.prices[variant.id]
        variant.price_source_usd_at_sync = variant.position.last_known_price_usd
        if variant.position_id == listing.position_id:
            listing.price_source_usd_at_sync = variant.position.last_known_price_usd
