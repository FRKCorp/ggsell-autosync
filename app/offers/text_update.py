"""Замена фразы в текстах уже созданных лотов (описание, инструкция) — без
перезалива: например, сменились часы поддержки.

Тексты берутся с витрины GGSell, а не из шаблонов: если клиент правил
описание лота вручную, его правки сохраняются — меняется только найденная
фраза. Трогаем только наши лоты (из таблицы listings), остальные товары
продавца не затрагиваются. Повторный запуск безопасен: где замен не
осталось, PATCH не отправляется.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.ggsell import GGSellError, GGSellV2Client, call_with_retry
from app.models.listing import Listing, ListingStatus

TEXT_FIELDS = ("description_ru", "description_en", "instructions_ru", "instructions_en")


@dataclass
class TextUpdateReport:
    updated: int = 0
    unchanged: int = 0
    missing_on_ggsell: int = 0
    errors: dict[int, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (f"обновлено={self.updated}, без изменений={self.unchanged}, "
                f"нет на GGSell={self.missing_on_ggsell}, ошибок={len(self.errors)}")


def replace_texts(offer: dict[str, Any], pairs: list[tuple[str, str]]) -> dict[str, str]:
    """Поля, в которых что-то заменилось, с новым текстом."""
    changed = {}
    for name in TEXT_FIELDS:
        text = offer.get(name)
        if not text:
            continue
        new = text
        for old, replacement in pairs:
            new = new.replace(old, replacement)
        if new != text:
            changed[name] = new
    return changed


def update_offer_texts(
    session: Session,
    v2: GGSellV2Client,
    offers: dict[int, dict[str, Any]],
    pairs: list[tuple[str, str]],
    *,
    dry_run: bool = False,
    pause_seconds: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Callable[[str], None] = lambda message: None,
) -> TextUpdateReport:
    """offers — {offer_id: оффер} из showcase.fetch_known_offers."""
    report = TextUpdateReport()
    listings = session.scalars(
        select(Listing).where(Listing.status != ListingStatus.ARCHIVED).order_by(Listing.id)
    ).all()
    for listing in listings:
        offer = offers.get(listing.ggsell_offer_id)
        if offer is None:
            report.missing_on_ggsell += 1
            continue
        changed = replace_texts(offer, pairs)
        if not changed:
            report.unchanged += 1
            continue
        if dry_run:
            report.updated += 1
            continue
        try:
            call_with_retry(v2.patch_offer, listing.ggsell_offer_id, changed, sleep=sleep, retry_network=True)
        except GGSellError as e:
            report.errors[listing.ggsell_offer_id] = f"{e.status_code}: {e.payload}"
            on_progress(f"❌ {listing.ggsell_offer_id}: {e.status_code}")
            continue
        report.updated += 1
        if report.updated % 100 == 0:
            on_progress(f"  обновлено {report.updated}…")
        if pause_seconds:
            sleep(pause_seconds)
    return report
