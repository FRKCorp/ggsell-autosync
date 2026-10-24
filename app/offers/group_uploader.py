"""Автозалив групповых лотов: один оффер на категорию FZ с параметром «Номинал».

Для каждой группы (app/offers/groups.py): собрать карточку (build_group_offer)
→ create_offer (черновик) → сразу записать Listing → параметр «Номинал» →
варианты номиналов → ListingVariant с id вариантов GGSell.

Повторный запуск безопасен: параметр ищется по названию, варианты — по
подписи, создаётся только недостающее. Так же досоздаются варианты для
номиналов, которые FZ добавил позже.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.ggsell import GGSellError, GGSellV2Client, call_with_retry
from app.models.listing import Listing, ListingStatus
from app.models.listing_variant import ListingVariant
from app.models.position import Position
from app.offers.builder import GroupOfferDraft, build_group_offer
from app.offers.groups import GroupSelection
from app.offers.options import options_list
from app.pricing.calculator import PricingConfig
from app.pricing.group_price import variant_payload


@dataclass
class GroupUploadReport:
    created: list[str] = field(default_factory=list)  # категории FZ, для которых создан лот
    variants_added: dict[str, int] = field(default_factory=dict)
    existing: int = 0
    empty: list[str] = field(default_factory=list)  # нет номиналов в продаже
    no_category: list[str] = field(default_factory=list)
    # Ещё есть отдельные лоты номиналов — сначала перевод (scripts/group_offers.py).
    pending_conversion: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"групповых лотов создано={len(self.created)}, уже были={self.existing}, "
            f"вариантов добавлено={sum(self.variants_added.values())}, без номиналов={len(self.empty)}, "
            f"без категории={len(self.no_category)}, ждут перевода={len(self.pending_conversion)}, "
            f"ошибок={len(self.errors)}"
        )


def single_listings(session: Session, fz_category_id: str) -> list[Listing]:
    """Отдельные (не групповые) лоты номиналов категории, которые ещё не удалены."""
    return list(session.scalars(
        select(Listing).join(Listing.position).where(
            Listing.is_group.is_(False),
            Position.fz_category_id == fz_category_id,
            Listing.status != ListingStatus.ARCHIVED,
        )
    ))


def find_group_listing(session: Session, fz_category_id: str) -> Optional[Listing]:
    return session.scalar(
        select(Listing).join(Listing.position).where(
            Listing.is_group.is_(True),
            Position.fz_category_id == fz_category_id,
            Listing.status != ListingStatus.ARCHIVED,
        )
    )


def _nominal_option(v2: GGSellV2Client, offer_id: int, title_ru: str) -> Optional[dict[str, Any]]:
    return next((o for o in options_list(v2.list_offer_options(offer_id)) if o.get("title_ru") == title_ru), None)


def ensure_nominal_option(v2: GGSellV2Client, offer_id: int, draft: GroupOfferDraft) -> dict[str, Any]:
    """Параметр «Номинал» на оффере: существующий (по названию) или новый."""
    option = _nominal_option(v2, offer_id, draft.option["title_ru"])
    if option is None:
        v2.create_or_update_options(offer_id, [draft.option])
        option = _nominal_option(v2, offer_id, draft.option["title_ru"])
        if option is None:
            raise RuntimeError(f"Параметр {draft.option['title_ru']!r} не найден на оффере {offer_id} после создания")
    return option


def sync_variants(
    session: Session, v2: GGSellV2Client, listing: Listing, option: dict[str, Any], draft: GroupOfferDraft
) -> int:
    """Создаёт на GGSell варианты номиналов, которых там нет (по подписи), и
    записывает ListingVariant для всех номиналов группы. Возвращает, сколько
    вариантов создано на GGSell."""
    on_ggsell = {v["title_ru"]: v for v in option.get("variants") or []}
    missing = [d for d in draft.variants if d.title_ru not in on_ggsell]
    if missing:
        has_default = any(v.get("is_default") for v in on_ggsell.values())
        payloads = []
        for d in missing:
            position_index = draft.variants.index(d)
            payloads.append({
                "title_ru": d.title_ru,
                "title_en": d.title_en,
                **variant_payload(None, d.price_rub, listing.price_rub),
                # Ровно один вариант по умолчанию — базовый номинал нового лота.
                "is_default": not has_default and position_index == 0,
                "status": "active",
                "position": position_index,
            })
        v2.create_or_update_variants(listing.ggsell_offer_id, option["id"], payloads)
        refreshed = _nominal_option(v2, listing.ggsell_offer_id, option["title_ru"]) or option
        on_ggsell = {v["title_ru"]: v for v in refreshed.get("variants") or []}

    known = {v.position_id for v in listing.variants}
    for index, d in enumerate(draft.variants):
        if d.position.id in known:
            continue
        created = on_ggsell.get(d.title_ru)
        if created is None:
            raise RuntimeError(f"Вариант {d.title_ru!r} не найден на оффере {listing.ggsell_offer_id} после создания")
        listing.variants.append(ListingVariant(
            position=d.position, ggsell_variant_id=created["id"], price_rub=d.price_rub,
            price_source_usd_at_sync=d.position.last_known_price_usd, sort_order=index,
        ))
    session.commit()
    return len(missing)


def upload_groups(
    session: Session,
    v2: GGSellV2Client,
    groups: dict[str, GroupSelection],
    category_map: dict[str, dict[str, Any]],
    config: PricingConfig,
    *,
    webhook_url: Optional[str],
    pause_seconds: float = 0.5,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Optional[Callable[[str], None]] = None,
) -> GroupUploadReport:
    report = GroupUploadReport()
    say = on_progress or (lambda message: None)
    for fz_category_id, selection in groups.items():
        if not selection.members:
            report.empty.append(fz_category_id)
            continue
        category = category_map.get(selection.base.external_id)
        if category is None:
            report.no_category.append(fz_category_id)
            continue
        listing = find_group_listing(session, fz_category_id)
        if listing is None and single_listings(session, fz_category_id):
            # Иначе на витрине окажутся и групповой лот, и старые отдельные.
            report.pending_conversion.append(fz_category_id)
            continue
        try:
            draft = build_group_offer(
                selection.members, category, config,
                option_title_ru=selection.rule.option_title_ru, option_title_en=selection.rule.option_title_en,
                webhook_url=webhook_url,
                markup_percent=listing.markup_percent if listing is not None else None,
            )
        except Exception as e:  # noqa: BLE001 — одна группа не должна валить залив
            report.errors[fz_category_id] = f"сборка карточки: {e}"
            continue
        if dry_run:
            if listing is None:
                report.created.append(fz_category_id)
            else:
                report.existing += 1
            continue

        if listing is None:
            try:
                offer = call_with_retry(v2.create_offer, draft.payload, sleep=sleep)
            except GGSellError as e:
                report.errors[fz_category_id] = f"create_offer {e.status_code}: {e.payload}"
                say(f"❌ {fz_category_id}: create_offer {e.status_code}")
                continue
            offer_id = (offer.get("data", offer) if isinstance(offer, dict) else offer)["id"]
            listing = Listing(
                position_id=selection.base.id,
                ggsell_offer_id=offer_id,
                status=ListingStatus.DRAFT,
                price_rub=draft.base.price_rub,
                price_source_usd_at_sync=selection.base.last_known_price_usd,
                ggsell_category_id=draft.category_id,
                ggsell_fee=draft.fees.fee,
                ggsell_payment_fee=draft.fees.payment_fee,
                is_group=True,
            )
            session.add(listing)
            session.commit()  # сразу: оффер уже существует на GGSell
            report.created.append(fz_category_id)
        else:
            report.existing += 1

        try:
            option = call_with_retry(ensure_nominal_option, v2, listing.ggsell_offer_id, draft, sleep=sleep)
            listing.ggsell_option_id = option["id"]
            session.commit()
            added = call_with_retry(sync_variants, session, v2, listing, option, draft, sleep=sleep)
            listing.options_attached = True
            session.commit()
        except Exception as e:  # noqa: BLE001
            session.rollback()
            report.errors[fz_category_id] = f"параметр/варианты (лот {listing.ggsell_offer_id}, повторный запуск досоздаст): {e}"
            say(f"⚠️ {fz_category_id}: лот {listing.ggsell_offer_id}, варианты не досозданы: {e}")
            continue
        if added:
            report.variants_added[fz_category_id] = added
        say(f"✅ {fz_category_id} → {listing.ggsell_offer_id}: {len(draft.variants)} номиналов"
            f"{f', добавлено вариантов {added}' if added else ''} (от {draft.base.price_rub} ₽)")
        if pause_seconds:
            sleep(pause_seconds)
    return report
