"""Снятие с продажи того, чего нет у FazerCards (режим «авто» в панели бота).

Номинал группового лота пропал у FZ, закончился или стал дороже лимита —
его вариант архивируется. Если это был базовый номинал, базой становится
самый дешёвый из доступных: цена лота, надбавки и вариант по умолчанию
пересчитываются одним запросом. Номинал вернулся — вариант создаётся заново:
архивный или скрытый вариант GGSell изменить не даёт (404 на любое изменение,
проверено 06.11). Возвращаем только снятое системой — вариант, скрытый
вручную, не трогаем.

Отдельный лот без товара у FZ (и групповой без единого доступного номинала) —
на паузу с пометкой auto_paused; товар вернулся — активируем только такие
лоты. Лот на проверке GGSell на паузу не ставится (API не даёт) — ждём.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.ggsell import GGSellError, GGSellV2Client, call_with_retry
from app.models.listing import Listing, ListingStatus
from app.models.listing_variant import ListingVariant
from app.models.position import Position
from app.offers.builder import nominal_title
from app.offers.group_conversion import wait_batch_job
from app.offers.groups import GroupRule, group_rule_for, sort_key
from app.offers.options import options_list
from app.pricing.calculator import PricingConfig
from app.pricing.group_price import ACTIVE, target_price, variant_payload

UNAVAILABLE = "unavailable"  # ListingVariant.status: снят системой (на GGSell — archived)


def is_available(position: Position, rule: Optional[GroupRule] = None) -> bool:
    price = position.last_known_price_usd
    if (position.fz_missing_since is not None or position.fz_out_of_stock_since is not None
            or not price or price <= 0):
        return False
    return rule is None or price <= rule.max_price_usd


@dataclass
class AvailabilityReport:
    hidden: list[str] = field(default_factory=list)  # номиналы групповых лотов
    restored: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)  # лоты целиком
    activated: list[str] = field(default_factory=list)
    errors: dict[int, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (f"номиналов скрыто={len(self.hidden)}, возвращено={len(self.restored)}, "
                f"лотов на паузу={len(self.paused)}, активировано={len(self.activated)}, ошибок={len(self.errors)}")

    def alert_text(self) -> Optional[str]:
        parts = []
        if self.hidden:
            parts.append(f"🙈 Скрыты номиналы ({len(self.hidden)}): " + ", ".join(self.hidden))
        if self.restored:
            parts.append(f"👁 Возвращены номиналы ({len(self.restored)}): " + ", ".join(self.restored))
        if self.paused:
            parts.append(f"⏸ На паузе ({len(self.paused)}): " + ", ".join(self.paused))
        if self.activated:
            parts.append(f"▶️ Снова в продаже ({len(self.activated)}, в защищённых категориях — после проверки "
                         f"GGSell): " + ", ".join(self.activated))
        if self.errors:
            parts.append(f"❗ Не удалось ({len(self.errors)}): "
                         + "; ".join(f"лот {i}: {e}" for i, e in self.errors.items()))
        return ("🤖 Наличие у FazerCards — авторежим:\n" + "\n".join(parts)) if parts else None


@dataclass
class _GroupPlan:
    base: ListingVariant
    base_price: Decimal
    prices: dict[int, Decimal]  # id ListingVariant → полная цена доступного номинала
    hide: list[ListingVariant]
    restore: list[ListingVariant]
    payload: list[dict[str, Any]]


def plan_group(listing: Listing, config: PricingConfig, rule: Optional[GroupRule]) -> Optional[_GroupPlan]:
    """Что поменять в вариантах группового лота; None — менять нечего или
    доступных номиналов нет вовсе (тогда лот целиком — на паузу)."""
    variants = sorted(listing.variants, key=lambda v: v.sort_order)
    available = [v for v in variants if v.status in (ACTIVE, UNAVAILABLE) and is_available(v.position, rule)]
    if not available:
        return None
    hide = [v for v in variants if v.status == ACTIVE and v not in available]
    restore = [v for v in available if v.status == UNAVAILABLE]
    base = min(available, key=lambda v: sort_key(v.position))
    if not hide and not restore and base.position_id == listing.position_id:
        return None
    prices = {v.id: target_price(listing, v, config) if v in restore else v.price_rub for v in available}
    base_price = prices[base.id]
    payload: list[dict[str, Any]] = [
        {"id": v.ggsell_variant_id, "status": "archived", "is_default": False}
        for v in hide if v.ggsell_variant_id is not None
    ]
    for v in available:
        if v in restore:
            title = nominal_title(v.position)
            payload.append({"title_ru": title, "title_en": title, **variant_payload(None, prices[v.id], base_price),
                            "is_default": v is base, "status": "active", "position": v.sort_order})
        elif v.ggsell_variant_id is not None:
            payload.append({**variant_payload(v.ggsell_variant_id, prices[v.id], base_price), "is_default": v is base})
    return _GroupPlan(base=base, base_price=base_price, prices=prices, hide=hide, restore=restore, payload=payload)


def _apply_group(
    session: Session, v2: GGSellV2Client, listing: Listing, plan: _GroupPlan, report: AvailabilityReport,
    sleep: Callable[[float], None],
) -> None:
    if plan.base_price != listing.price_rub:
        call_with_retry(v2.patch_offer, listing.ggsell_offer_id, {"price": float(plan.base_price)},
                        sleep=sleep, retry_network=True)
    call_with_retry(v2.create_or_update_variants, listing.ggsell_offer_id, listing.ggsell_option_id, plan.payload,
                    sleep=sleep)
    new_ids: dict[str, int] = {}
    if plan.restore:
        known = {v.ggsell_variant_id for v in listing.variants}
        response = call_with_retry(v2.list_offer_options, listing.ggsell_offer_id, sleep=sleep, retry_network=True)
        option = next((o for o in options_list(response) if o.get("id") == listing.ggsell_option_id), {})
        new_ids = {v["title_ru"]: v["id"] for v in option.get("variants") or []
                   if v.get("status", ACTIVE) == ACTIVE and v["id"] not in known}

    for v in plan.hide:
        v.status = UNAVAILABLE
        report.hidden.append(v.position.name)
    for v in plan.restore:
        v.ggsell_variant_id = new_ids.get(nominal_title(v.position))
        v.status = ACTIVE
        report.restored.append(v.position.name)
    for v_id, price in plan.prices.items():
        variant = next(v for v in listing.variants if v.id == v_id)
        if variant.price_rub != price:
            variant.price_rub = price
            variant.price_source_usd_at_sync = variant.position.last_known_price_usd
    listing.position_id = plan.base.position_id
    listing.price_rub = plan.base_price
    listing.price_source_usd_at_sync = plan.base.position.last_known_price_usd
    session.commit()


def _run_batch(v2: GGSellV2Client, batch: Callable[[list[int]], Any], listings: list[Listing],
               sleep: Callable[[float], None]) -> None:
    ids = [l.ggsell_offer_id for l in listings]
    for start in range(0, len(ids), v2.BATCH_LIMIT):
        chunk = ids[start:start + v2.BATCH_LIMIT]
        wait_batch_job(v2, call_with_retry(batch, chunk, sleep=sleep), len(chunk), sleep=sleep)


def _title(listing: Listing) -> str:
    return listing.position.name.split(" — ", 1)[0] if listing.is_group else listing.position.name


def apply_availability(
    session: Session, v2: GGSellV2Client, config: PricingConfig, sleep: Callable[[float], None] = time.sleep,
) -> AvailabilityReport:
    report = AvailabilityReport()
    to_pause: list[Listing] = []
    to_activate: list[Listing] = []
    listings = session.scalars(
        select(Listing).where(Listing.status != ListingStatus.ARCHIVED).order_by(Listing.id)
    ).all()
    for listing in listings:
        if listing.is_group:
            rule = group_rule_for(listing.position.fz_category_id)
            selling = any(v.status in (ACTIVE, UNAVAILABLE) and is_available(v.position, rule)
                          for v in listing.variants)
        else:
            selling = is_available(listing.position)
        if not selling:
            if listing.status == ListingStatus.ACTIVE:
                to_pause.append(listing)
            continue
        if listing.auto_paused:
            if listing.status == ListingStatus.PAUSED:
                to_activate.append(listing)
            else:  # уже активировали вручную
                listing.auto_paused = False
                session.commit()
        if not listing.is_group or listing.ggsell_option_id is None:
            continue
        plan = plan_group(listing, config, rule)
        if plan is None:
            continue
        try:
            _apply_group(session, v2, listing, plan, report, sleep)
        except (GGSellError, httpx.TransportError, RuntimeError) as e:
            session.rollback()
            report.errors[listing.ggsell_offer_id] = str(e)[:200]

    for listings_, batch, status, auto_paused, names in (
        (to_pause, v2.batch_pause_offers, ListingStatus.PAUSED, True, report.paused),
        (to_activate, v2.batch_activate_offers, ListingStatus.ACTIVE, False, report.activated),
    ):
        if not listings_:
            continue
        try:
            _run_batch(v2, batch, listings_, sleep)
        except (GGSellError, httpx.TransportError, RuntimeError) as e:
            for listing in listings_:
                report.errors[listing.ggsell_offer_id] = str(e)[:200]
            continue
        for listing in listings_:
            listing.status = status  # статус после проверки GGSell подтянет sync_listings
            listing.auto_paused = auto_paused
            names.append(_title(listing))
        session.commit()
    return report
