"""Перевод отдельных лотов номиналов в групповые лоты (scripts/group_offers.py).

По каждой групповой категории FZ (data/grouped_categories.json): лот
минимального номинала становится групповым — новые название, описание,
инструкция и цена (обложка, категория и отзывы остаются), параметр
«Номинал» и варианты; остальные лоты категории — на паузу (или удаляются),
в БД — archived.

Повторный запуск безопасен: групповой лот только досоздаёт варианты, лоты
на паузе и удалённые не трогаются. Отдельные лоты убираются, только когда
у группового лота все варианты на месте — иначе номинал нельзя было бы купить.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.ggsell import GGSellError, GGSellV2Client, call_with_retry
from app.models.listing import Listing, ListingStatus
from app.models.position import Position
from app.offers.builder import GroupOfferDraft, build_group_offer
from app.offers.group_uploader import attach_nominals, find_group_listing
from app.offers.groups import GroupSelection
from app.pricing.calculator import PricingConfig

PAUSE = "pause"
DELETE = "delete"
# Статусы GGSell, в которых лот не продаётся — ставить на паузу нечего.
NOT_SELLING = {"draft", "paused"}
TEXT_FIELDS = ("title_ru", "title_en", "description_ru", "description_en", "instructions_ru", "instructions_en")


@dataclass
class ConversionReport:
    converted: dict[str, int] = field(default_factory=dict)  # категория FZ → лот, ставший групповым
    existing: int = 0
    variants_added: dict[str, int] = field(default_factory=dict)
    retired: dict[str, list[int]] = field(default_factory=dict)  # категория FZ → лоты на паузу/удалённые
    over_limit: dict[str, int] = field(default_factory=dict)  # номиналов дороже лимита
    nothing: list[str] = field(default_factory=list)  # нет лотов — групповой создаст upload_offers.py
    empty: list[str] = field(default_factory=list)  # нет номиналов в продаже
    errors: dict[str, str] = field(default_factory=dict)

    def summary(self, mode: str = PAUSE) -> str:
        action = "удалено" if mode == DELETE else "на паузу"
        return (
            f"переведено в групповые={len(self.converted)}, уже групповые={self.existing}, "
            f"вариантов добавлено={sum(self.variants_added.values())}, "
            f"отдельных лотов {action}={sum(len(ids) for ids in self.retired.values())}, "
            f"без лотов={len(self.nothing)}, без номиналов={len(self.empty)}, ошибок={len(self.errors)}"
        )


def category_singles(session: Session, fz_category_id: str) -> list[Listing]:
    """Все отдельные лоты номиналов категории, в том числе archived в БД:
    лот на паузе ещё есть на GGSell, и --delete его удалит."""
    return list(session.scalars(
        select(Listing).join(Listing.position)
        .where(Listing.is_group.is_(False), Position.fz_category_id == fz_category_id)
        .order_by(Listing.id)
    ))


def pick_lot(listings: list[Listing], selection: GroupSelection) -> Optional[Listing]:
    """Лот, который станет групповым: минимального номинала из группы, если
    такого нет — самого дешёвого из оставшихся."""
    order = {p.id: i for i, p in enumerate(selection.members)}
    return min(
        listings,
        key=lambda l: (l.status == ListingStatus.ARCHIVED, order.get(l.position_id, len(order)),
                       l.position.last_known_price_usd or 0, l.id),
        default=None,
    )


def lot_category(listing: Listing, category_map: dict[str, dict[str, Any]], base: Position) -> Optional[dict[str, Any]]:
    """Категория и комиссии — с самого лота (синхронизация берёт их с витрины),
    карта категорий — если лот ещё ни разу не синхронизировался."""
    mapped = category_map.get(base.external_id) or category_map.get(listing.position.external_id)
    if listing.ggsell_category_id is None or not (listing.ggsell_fee or listing.ggsell_payment_fee):
        if mapped is None:
            return None
        return {**mapped, "category_id": listing.ggsell_category_id or mapped["category_id"]}
    return {"category_id": listing.ggsell_category_id, "fee": listing.ggsell_fee,
            "payment_fee": listing.ggsell_payment_fee}


def text_payload(draft: GroupOfferDraft) -> dict[str, Any]:
    """PATCH лота при переводе: тексты и цена базового номинала, без обложки."""
    return {**{k: draft.payload[k] for k in TEXT_FIELDS}, "price": draft.payload["price"]}


def wait_batch_job(
    v2: GGSellV2Client, response: Any, expected: int, sleep: Callable[[float], None] = time.sleep,
    attempts: int = 20, delay: float = 1.5,
) -> None:
    """batch-методы GGSell асинхронны: ждём результат job и проверяем, что
    обработаны все лоты. Иначе — исключение, БД не трогаем."""
    body = response.get("data", response) if isinstance(response, dict) else {}
    job_id = body.get("job_id") if isinstance(body, dict) else None
    if not job_id:
        return
    for _ in range(attempts):
        sleep(delay)
        result = call_with_retry(v2.get_async_job_result, job_id, sleep=sleep, retry_network=True)
        result = result.get("data", result) if isinstance(result, dict) else {}
        status = result.get("status")
        if status in ("failed", "error"):
            raise RuntimeError(f"GGSell: задача {job_id} завершилась ошибкой: {result}")
        if status == "completed":
            done = (result.get("results") or {}).get("success_count")
            if done is not None and done < expected:
                raise RuntimeError(f"GGSell обработал {done} из {expected} лотов (задача {job_id}), повторите запуск")
            return
    raise RuntimeError(f"GGSell не завершил задачу {job_id} — проверьте лоты и повторите запуск")


def retire_offers(
    v2: GGSellV2Client, offer_ids: list[int], mode: str, sleep: Callable[[float], None] = time.sleep,
) -> None:
    batch = v2.batch_delete_offers if mode == DELETE else v2.batch_pause_offers
    for start in range(0, len(offer_ids), v2.BATCH_LIMIT):
        chunk = offer_ids[start:start + v2.BATCH_LIMIT]
        response = call_with_retry(batch, chunk, sleep=sleep)
        wait_batch_job(v2, response, len(chunk), sleep=sleep)


def convert_groups(
    session: Session,
    v2: GGSellV2Client,
    groups: dict[str, GroupSelection],
    offers: dict[int, dict[str, Any]],
    category_map: dict[str, dict[str, Any]],
    config: PricingConfig,
    *,
    webhook_url: Optional[str],
    mode: str = PAUSE,
    dry_run: bool = False,
    pause_seconds: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
    on_progress: Optional[Callable[[str], None]] = None,
) -> ConversionReport:
    """offers — лоты с витрины GGSell (showcase.fetch_offers): по ним видно,
    какие лоты ещё существуют и в каком статусе."""
    if mode not in (PAUSE, DELETE):
        raise ValueError(f"mode: {PAUSE!r} или {DELETE!r}, не {mode!r}")
    report = ConversionReport()
    say = on_progress or (lambda message: None)
    for fz_category_id, selection in groups.items():
        if not selection.members:
            report.empty.append(fz_category_id)
            continue
        group = find_group_listing(session, fz_category_id)
        singles = category_singles(session, fz_category_id)
        live = [l for l in singles if l.ggsell_offer_id in offers]
        target = group or pick_lot(live, selection)
        if target is None:
            report.nothing.append(fz_category_id)
            continue
        if selection.over_limit:
            report.over_limit[fz_category_id] = len(selection.over_limit)
        others = [l for l in live if l is not target]
        to_retire = [l.ggsell_offer_id for l in others
                     if mode == DELETE or offers[l.ggsell_offer_id].get("status") not in NOT_SELLING]

        category = lot_category(target, category_map, selection.base)
        if category is None:
            report.errors[fz_category_id] = f"лот {target.ggsell_offer_id}: нет категории GGSell ни у лота, ни в карте"
            continue
        try:
            draft = build_group_offer(
                selection.members, category, config,
                option_title_ru=selection.rule.option_title_ru, option_title_en=selection.rule.option_title_en,
                webhook_url=webhook_url, markup_percent=target.markup_percent,
            )
        except Exception as e:  # noqa: BLE001 — одна категория не должна валить перевод
            report.errors[fz_category_id] = f"сборка карточки: {e}"
            continue
        if dry_run:
            if group is None:
                report.converted[fz_category_id] = target.ggsell_offer_id
            else:
                report.existing += 1
            if to_retire:
                report.retired[fz_category_id] = to_retire
            continue

        if group is None:
            try:
                call_with_retry(v2.patch_offer, target.ggsell_offer_id, text_payload(draft), sleep=sleep)
            except GGSellError as e:
                report.errors[fz_category_id] = f"лот {target.ggsell_offer_id}, PATCH {e.status_code}: {e.payload}"
                say(f"❌ {fz_category_id}: лот {target.ggsell_offer_id} не переведён ({e.status_code})")
                continue
            target.is_group = True
            target.position_id = selection.base.id
            target.price_rub = draft.base.price_rub
            target.price_source_usd_at_sync = selection.base.last_known_price_usd
            session.commit()
            report.converted[fz_category_id] = target.ggsell_offer_id
        else:
            report.existing += 1

        try:
            added = attach_nominals(session, v2, target, draft, sleep=sleep)
        except Exception as e:  # noqa: BLE001
            session.rollback()
            report.errors[fz_category_id] = (f"параметр/варианты (лот {target.ggsell_offer_id}; отдельные лоты не "
                                             f"тронуты, повторный запуск досоздаст): {e}")
            say(f"⚠️ {fz_category_id}: лот {target.ggsell_offer_id}, варианты не досозданы: {e}")
            continue
        if added:
            report.variants_added[fz_category_id] = added

        if to_retire:
            try:
                retire_offers(v2, to_retire, mode, sleep=sleep)
            except Exception as e:  # noqa: BLE001
                report.errors[fz_category_id] = f"групповой лот {target.ggsell_offer_id} готов, отдельные лоты: {e}"
                say(f"⚠️ {fz_category_id}: отдельные лоты не убраны: {e}")
                continue
            report.retired[fz_category_id] = to_retire
        for listing in singles:
            if listing is not target and listing.status != ListingStatus.ARCHIVED:
                listing.status = ListingStatus.ARCHIVED
        session.commit()
        say(f"✅ {fz_category_id} → {target.ggsell_offer_id}: {len(draft.variants)} номиналов (от {draft.base.price_rub} ₽)"
            f"{f', добавлено вариантов {added}' if added else ''}"
            f"{f', отдельных лотов убрано {len(to_retire)}' if to_retire else ''}")
        if pause_seconds:
            sleep(pause_seconds)
    return report
