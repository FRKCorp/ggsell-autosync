"""Автозалив лотов на GGSell.

Для каждой позиции из data/ggsell_category_map.json (без категории — не
выставляем, решение 2.13): собрать карточку (builder.py) → create_offer
(черновик) → **сразу записать Listing** → повесить опции покупателя
(options.py, только топапы) → отметить options_attached.

Повторяемость:
  - позиция, у которой уже есть Listing, заново не создаётся;
  - лот есть, а опции не повешены (сбой посередине) — повторный запуск
    досоздаёт только опции (attach_topup_options сам пропускает уже
    существующие);
  - ошибка на одной позиции не останавливает остальные.
Узкое место: падение процесса ровно между create_offer и коммитом Listing —
тогда повторный запуск создаст второй оффер. Окно — миллисекунды; лот
коммитится сразу после ответа GGSell.

Лоты создаются только черновиками: обложки добавляет и публикует клиент
(решение 2.6).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsClient, FazerCardsError
from app.clients.ggsell import GGSellError, GGSellV2Client, call_with_retry
from app.models.listing import Listing, ListingStatus
from app.models.position import Position, SourceType
from app.offers.builder import build_offer
from app.offers.categories import SPECIAL_CATEGORIES
from app.offers.options import attach_topup_options, buyer_fields_for
from app.pricing.calculator import PricingConfig

logger = logging.getLogger(__name__)


def default_webhook_url() -> Optional[str]:
    """Адрес вебхука GGSell для новых лотов: GGSELL_WEBHOOK_URL, а если не
    задан — https://<первый домен из DOMAIN>/webhooks/ggsell."""
    explicit = os.getenv("GGSELL_WEBHOOK_URL", "").strip()
    if explicit:
        return explicit
    domain = os.getenv("DOMAIN", "").split(",")[0].strip()
    return f"https://{domain}/webhooks/ggsell" if domain and domain != "localhost" else None


@dataclass
class UploadReport:
    created: list[str] = field(default_factory=list)
    options_fixed: list[str] = field(default_factory=list)  # досоздали опции у ранее созданного лота
    skipped_existing: int = 0
    skipped_no_category: int = 0
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"создано={len(self.created)}, досоздано опций={len(self.options_fixed)}, "
            f"уже были={self.skipped_existing}, без категории={self.skipped_no_category}, "
            f"ошибок={len(self.errors)}"
        )


class FieldsSource:
    """Поля покупателя FZ по категории: живьём из get_topup_offers (по одному
    запросу на категорию), при ошибке — из data/fz_topup_fields.json."""

    def __init__(self, fz_client: Optional[FazerCardsClient], fallback: dict[str, list[dict[str, Any]]]):
        self._fz = fz_client
        self._fallback = fallback
        self._cache: dict[str, list[dict[str, Any]]] = {}

    def get(self, category_id: str) -> list[dict[str, Any]]:
        if category_id not in self._cache:
            fields = None
            if self._fz is not None:
                try:
                    fields = self._fz.get_topup_offers(category_id).get("fields")
                except FazerCardsError as e:
                    logger.warning("Поля FZ для %s не получены (%s) — беру из файла", category_id, e)
            self._cache[category_id] = fields if fields is not None else self._fallback.get(category_id, [])
        return self._cache[category_id]


def upload_positions(
    session: Session,
    v2: GGSellV2Client,
    positions: Iterable[Position],
    category_map: dict[str, dict[str, Any]],
    config: PricingConfig,
    fields: FieldsSource,
    *,
    webhook_url: Optional[str],
    pause_seconds: float = 0.5,
    limit: Optional[int] = None,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Optional[Callable[[str], None]] = None,
) -> UploadReport:
    """Залить черновики для positions. limit — максимум НОВЫХ лотов за запуск.
    dry_run — только собрать карточки, без запросов к GGSell и записи в БД."""
    report = UploadReport()
    say = on_progress or (lambda message: None)
    for position in positions:
        if limit is not None and len(report.created) >= limit:
            break
        category = category_map.get(position.external_id) or SPECIAL_CATEGORIES.get(position.external_id)
        if category is None:
            report.skipped_no_category += 1
            continue

        listing = session.scalar(select(Listing).where(Listing.position_id == position.id))
        is_topup = SourceType(position.source_type) == SourceType.TOPUP
        # Поля покупателя: у топапов — от FZ, у Steam/Telegram — свои.
        special_fields = buyer_fields_for(position.source_type)
        needs_options = is_topup or bool(special_fields)
        if listing is not None:
            if listing.options_attached or not needs_options:
                report.skipped_existing += 1
                continue
            # Лот есть, опции не повешены — досоздаём только их.
            if dry_run:
                report.options_fixed.append(position.external_id)
                continue
            try:
                call_with_retry(
                    attach_topup_options, v2, listing.ggsell_offer_id,
                    special_fields or fields.get(position.fz_category_id), sleep=sleep,
                )
                listing.options_attached = True
                session.commit()
                report.options_fixed.append(position.external_id)
                say(f"🔧 {position.external_id}: опции досозданы (оффер {listing.ggsell_offer_id})")
            except Exception as e:  # noqa: BLE001 — одна позиция не должна валить залив
                session.rollback()
                report.errors[position.external_id] = f"опции: {e}"
                say(f"❌ {position.external_id}: опции: {e}")
            continue

        fz_fields = special_fields or (fields.get(position.fz_category_id) if is_topup else [])
        try:
            draft = build_offer(position, category, config, webhook_url=webhook_url, fz_fields=fz_fields)
        except Exception as e:  # noqa: BLE001
            report.errors[position.external_id] = f"сборка карточки: {e}"
            continue
        if dry_run:
            report.created.append(position.external_id)
            continue

        try:
            offer = call_with_retry(v2.create_offer, draft.payload, sleep=sleep)
        except GGSellError as e:
            report.errors[position.external_id] = f"create_offer {e.status_code}: {e.payload}"
            say(f"❌ {position.external_id}: create_offer {e.status_code}")
            continue
        offer_id = (offer.get("data", offer) if isinstance(offer, dict) else offer)["id"]

        listing = Listing(
            position_id=position.id,
            ggsell_offer_id=offer_id,
            status=ListingStatus.DRAFT,
            price_rub=draft.price_rub,
            price_source_usd_at_sync=draft.price_usd,
            ggsell_category_id=draft.category_id,
            ggsell_fee=draft.fees.fee,
            ggsell_payment_fee=draft.fees.payment_fee,
            options_attached=not draft.fz_fields,
        )
        session.add(listing)
        session.commit()  # сразу: оффер уже существует на GGSell
        report.created.append(position.external_id)

        if draft.fz_fields:
            try:
                call_with_retry(attach_topup_options, v2, offer_id, draft.fz_fields, sleep=sleep)
                listing.options_attached = True
                session.commit()
            except Exception as e:  # noqa: BLE001
                session.rollback()
                report.errors[position.external_id] = f"опции (лот {offer_id} создан, повторный запуск досоздаст): {e}"
                say(f"⚠️ {position.external_id}: оффер {offer_id} создан, опции не повесились: {e}")
        say(f"✅ {position.external_id} → {offer_id} ({draft.price_rub} ₽)")
        if pause_seconds:
            sleep(pause_seconds)
    return report
