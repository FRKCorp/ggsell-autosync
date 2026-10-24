"""Витрина GGSell ⇄ наша БД после обновления цен FZ.

  - sync_listings — статус и цена лотов с GGSell (4.5a): лоты публикует и
    правит клиент сам (решение 2.6). Лот, которого нет в выдаче list_offers
    (архивные туда не попадают), считается удалённым → archived, цены по нему
    больше не отправляем. Цена с витрины — чтобы сравнивать с тем, что видит
    покупатель: цену, изменённую вручную, прайсер вернёт к расчётной (для
    отдельного лота цена меняется индивидуальной наценкой, 3.4b).
  - check_availability — пропало у FZ / закончился остаток (4.3, 4.5):
    решение клиента — только алерт, лот не трогаем. Каждое событие —
    один раз («пропало» → «вернулось»), по датам в Position.
  - push_prices — пересчёт цены каждого лота и PATCH только поля price (4.1,
    4.2): обложки и тексты правит клиент, их не трогаем. Чтобы не дёргать
    2000+ лотов из-за копеечных колебаний курса — PATCH, только если цена
    изменилась больше порога (по умолчанию 1%), либо если текущая цена уже
    не даёт минимальную маржу (тогда — сразу и с алертом).
"""

from __future__ import annotations

import logging
import os
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.ggsell import GGSellError, GGSellV2Client, call_with_retry
from app.models.listing import Listing, ListingStatus
from app.models.listing_variant import ListingVariant
from app.models.position import Position, SourceType
from app.offers.options import options_list
from app.orders.notifications import notify_admin
from app.pricing.calculator import GGSellFees, PricingConfig, is_margin_safe
from app.pricing.group_price import apply_group_update, plan_group_update
from app.pricing.listing_price import listing_price_rub
from app.sync.fz_catalog import is_gone_error, is_unexpected_fz_error

logger = logging.getLogger(__name__)

DEFAULT_PRICE_CHANGE_THRESHOLD_PERCENT = Decimal("1")
ALERT_LIST_LIMIT = 30  # сколько позиций перечислять в одном алерте


def _short_list(names: list[str]) -> str:
    shown = "\n".join(f"  • {name}" for name in names[:ALERT_LIST_LIMIT])
    rest = len(names) - ALERT_LIST_LIMIT
    return shown + (f"\n  … и ещё {rest}" if rest > 0 else "")


# ----------------------------------------------------------------------
# 4.5a — статусы лотов с GGSell
# ----------------------------------------------------------------------


def fetch_offers(v2: GGSellV2Client, sleep: Callable[[float], None] = time.sleep) -> dict[int, dict[str, Any]]:
    """{offer_id: оффер} по всем страницам list_offers (100 на страницу,
    pagination.has_next_page). Архивные офферы в выдачу не попадают."""
    offers: dict[int, dict[str, Any]] = {}
    page = 1
    while True:
        response = call_with_retry(v2.list_offers, page=page, sleep=sleep, retry_network=True)
        for offer in response.get("data", []):
            offers[offer["id"]] = offer
        if not response.get("pagination", {}).get("has_next_page"):
            return offers
        page += 1


def sync_listings(session: Session, offers: dict[int, dict[str, Any]]) -> dict[str, int]:
    """Комиссии GGSell тоже берём с витрины: GGSell меняет их без
    предупреждения (Delta Coins: 2% → 4%), а цена лота закладывает комиссию."""
    status_changed = archived = price_changed = fee_changed = 0
    for listing in session.scalars(select(Listing).where(Listing.status != ListingStatus.ARCHIVED)):
        offer = offers.get(listing.ggsell_offer_id)
        if offer is None:
            logger.warning("Лот %s удалён на GGSell — помечаю archived", listing.ggsell_offer_id)
            listing.status = ListingStatus.ARCHIVED
            archived += 1
            continue
        if offer["status"] != listing.status:
            listing.status = offer["status"]
            status_changed += 1
        if offer.get("currency", "RUB") == "RUB" and offer.get("price") is not None:
            live_price = Decimal(str(offer["price"]))
            if live_price != listing.price_rub:
                logger.info("Лот %s: цена на GGSell %s, у нас записана %s — беру с витрины",
                            listing.ggsell_offer_id, live_price, listing.price_rub)
                listing.price_rub = live_price
                price_changed += 1
        category = offer.get("category") or {}
        if category.get("fee") is not None and category.get("payment_fee") is not None:
            fee = Decimal(str(category["fee"]))
            payment_fee = Decimal(str(category["payment_fee"]))
            if (fee, payment_fee) != (listing.ggsell_fee, listing.ggsell_payment_fee):
                logger.info("Лот %s: комиссия GGSell %s+%s, у нас записана %s+%s — беру с витрины",
                            listing.ggsell_offer_id, fee, payment_fee, listing.ggsell_fee, listing.ggsell_payment_fee)
                listing.ggsell_fee, listing.ggsell_payment_fee = fee, payment_fee
                fee_changed += 1
    session.commit()
    return {"status_changed": status_changed, "archived": archived, "price_changed_on_ggsell": price_changed,
            "fee_changed": fee_changed}


# ----------------------------------------------------------------------
# 4.3, 4.5 — пропало у FZ / закончилось
# ----------------------------------------------------------------------


FZ_ERRORS_ALERT_SHARE = Decimal("0.1")  # алерт, если не обновилось ≥ 10% позиций


def fz_errors_alert(refresh_results: list[tuple[Position, Any, Optional[str]]]) -> Optional[str]:
    """Текст алерта, если FZ массово не отвечает (не «пропало», а сбой:
    403 подписка, 5xx, сеть) — цены тогда считаются по устаревшим данным.
    Единичные временные сбои категорий не алертим — следующий запуск догонит.
    Выяснилось 03.10: подписка FZ протухла, и 2519 из 2519 позиций падали
    молча, только в лог."""
    failed = [error for _, _, error in refresh_results if error and not is_gone_error(error)]
    if not failed or len(failed) < len(refresh_results) * FZ_ERRORS_ALERT_SHARE:
        return None
    top = Counter(failed).most_common(3)
    lines = "\n".join(f"  • {error} — {count}" for error, count in top)
    hint = ("\nПохоже, истекла подписка FazerCards — продлите её в кабинете FZ."
            if any("403" in error for error, _ in top) else "")
    return (f"❗ FazerCards: цены не обновились у {len(failed)} из {len(refresh_results)} позиций — "
            f"цены на витрине считаются по последним известным.\n{lines}{hint}")


def check_availability(
    session: Session,
    refresh_results: Iterable[tuple[Position, Any, Optional[str]]],
    now: Optional[datetime] = None,
    alert: Callable[[str], None] = notify_admin,
) -> dict[str, int]:
    """refresh_results — результат refresh_all_positions: (позиция, результат,
    ошибка). Ошибка «не найден / категория 404» = позиция пропала у FZ
    (is_gone_error). Алертим только по позициям с лотом на GGSell (остальные не продаются),
    и только при смене состояния."""
    now = now or datetime.now(timezone.utc)
    listed = set(session.scalars(
        select(Listing.position_id).where(Listing.status != ListingStatus.ARCHIVED)
    )) | set(session.scalars(  # номиналы групповых лотов
        select(ListingVariant.position_id).join(Listing)
        .where(Listing.status != ListingStatus.ARCHIVED, ListingVariant.status == "active")
    ))
    gone, back, out, restocked, unexpected = [], [], [], [], []
    for position, result, error in refresh_results:
        is_listed = position.id in listed
        if error:
            # Цена 0 / не число / нет поля — цену не меняли; сообщаем при каждой
            # синхронизации, пока FZ отдаёт такое (это не «пропало»).
            if is_unexpected_fz_error(error) and is_listed:
                unexpected.append(f"{position.name} — {error}")
            # Временный сбой FZ (5xx, 429, сеть) — состояние не меняем.
            if is_gone_error(error) and position.fz_missing_since is None:
                position.fz_missing_since = now
                if is_listed:
                    gone.append(f"{position.name} — {error}")
            continue
        if position.fz_missing_since is not None:
            position.fz_missing_since = None
            if is_listed:
                back.append(position.name)
        if SourceType(position.source_type) == SourceType.GIFTCARD:
            stock = (position.raw_payload or {}).get("stock")
            if stock == 0 and position.fz_out_of_stock_since is None:
                position.fz_out_of_stock_since = now
                if is_listed:
                    out.append(position.name)
            elif stock and position.fz_out_of_stock_since is not None:
                position.fz_out_of_stock_since = None
                if is_listed:
                    restocked.append(position.name)
    session.commit()

    if gone:
        alert(f"❗ Пропало у FazerCards ({len(gone)}) — лоты на GGSell не тронуты, решите вручную:\n{_short_list(gone)}")
    if out:
        alert(f"⚠️ Закончилось у FazerCards ({len(out)}) — лоты на GGSell не тронуты:\n{_short_list(out)}")
    if back:
        alert(f"✅ Снова есть у FazerCards ({len(back)}):\n{_short_list(back)}")
    if restocked:
        alert(f"✅ Снова в наличии у FazerCards ({len(restocked)}):\n{_short_list(restocked)}")
    if unexpected:
        alert(f"❗ FazerCards вернул неправильные данные ({len(unexpected)}) — цены этих лотов не изменены. "
              f"При покупке цена перепроверяется: если ответ всё ещё неправильный, заказ уйдёт на ручной "
              f"разбор, товар не покупается:\n{_short_list(unexpected)}")
    return {"gone": len(gone), "back": len(back), "out_of_stock": len(out), "restocked": len(restocked),
            "unexpected": len(unexpected)}


# ----------------------------------------------------------------------
# 4.1, 4.2 — цены на витрину
# ----------------------------------------------------------------------


@dataclass
class PushReport:
    patched: int = 0
    unchanged: int = 0
    skipped_below_threshold: int = 0
    margin_fixes: list[str] = field(default_factory=list)  # лоты, где цена уже не давала мин. маржу
    deleted_on_ggsell: int = 0
    errors: dict[int, str] = field(default_factory=dict)
    variants_patched: int = 0  # надбавки вариантов групповых лотов

    def summary(self) -> str:
        return (
            f"обновлено={self.patched}, без изменений={self.unchanged}, "
            f"ниже порога={self.skipped_below_threshold}, защита маржи={len(self.margin_fixes)}, "
            f"удалены на GGSell={self.deleted_on_ggsell}, ошибок={len(self.errors)}"
            + (f", вариантов групповых лотов={self.variants_patched}" if self.variants_patched else "")
        )


def price_change_threshold() -> Decimal:
    return Decimal(os.getenv("PRICE_PUSH_THRESHOLD_PERCENT", str(DEFAULT_PRICE_CHANGE_THRESHOLD_PERCENT)))


def push_prices(
    session: Session,
    v2: GGSellV2Client,
    config: PricingConfig,
    *,
    threshold_percent: Optional[Decimal] = None,
    pause_seconds: float = 0.2,
    sleep: Callable[[float], None] = time.sleep,
    alert: Callable[[str], None] = notify_admin,
    listings: Optional[list[Listing]] = None,
) -> PushReport:
    """listings — только эти лоты (проверка scripts/check_zero_price.py); по умолчанию — все."""
    threshold = threshold_percent if threshold_percent is not None else price_change_threshold()
    report = PushReport()
    if listings is None:
        listings = session.scalars(
            select(Listing).where(Listing.status != ListingStatus.ARCHIVED).order_by(Listing.id)
        ).all()
    for listing in listings:
        if listing.is_group:
            _push_group(session, v2, listing, config, threshold, report, sleep)
            if pause_seconds:
                sleep(pause_seconds)
            continue
        position = listing.position
        if position.fz_missing_since is not None or not position.last_known_price_usd:
            report.unchanged += 1  # у FZ позиции нет — цену поставщика не знаем
            continue
        new_price = listing_price_rub(listing, config)
        old_price = listing.price_rub
        if new_price == old_price:
            report.unchanged += 1
            continue

        fees = GGSellFees(fee=listing.ggsell_fee, payment_fee=listing.ggsell_payment_fee)
        margin_unsafe = not is_margin_safe(
            old_price, position.last_known_price_usd, config.exchange_rate_usd_to_rub, config, fees
        )
        change_percent = abs(new_price - old_price) / old_price * 100 if old_price else Decimal(100)
        if not margin_unsafe and change_percent < threshold:
            report.skipped_below_threshold += 1
            continue

        try:
            call_with_retry(v2.patch_offer, listing.ggsell_offer_id, {"price": float(new_price)}, sleep=sleep,
                            retry_network=True)
        except GGSellError as e:
            if e.status_code == 404:
                listing.status = ListingStatus.ARCHIVED
                report.deleted_on_ggsell += 1
            else:
                report.errors[listing.ggsell_offer_id] = f"{e.status_code}: {e.payload}"
            session.commit()
            continue
        listing.price_rub = new_price
        listing.price_source_usd_at_sync = position.last_known_price_usd
        session.commit()
        report.patched += 1
        if margin_unsafe:
            report.margin_fixes.append(f"{position.name}: {old_price} → {new_price} ₽")
        if pause_seconds:
            sleep(pause_seconds)

    if report.margin_fixes:
        alert(
            f"⚠️ У {len(report.margin_fixes)} лотов (номиналов) цена на GGSell давала маржу ниже "
            f"{config.min_margin_percent}% (выросла цена поставщика или цену меняли вручную) — "
            f"цены пересчитаны:\n{_short_list(report.margin_fixes)}"
        )
    if report.errors:
        alert(f"❗ Не удалось обновить цену у {len(report.errors)} лотов на GGSell — см. логи.")
    return report


# ----------------------------------------------------------------------
# Групповые лоты: надбавки вариантов «Номинал»
# ----------------------------------------------------------------------


def _push_group(
    session: Session,
    v2: GGSellV2Client,
    listing: Listing,
    config: PricingConfig,
    threshold: Decimal,
    report: PushReport,
    sleep: Callable[[float], None],
) -> None:
    """Цена лота (базовый номинал) и надбавки вариантов. Если что-то не
    отправилось, в БД не пишем ничего: следующая синхронизация прочитает
    фактическое состояние с витрины (sync_group_variants) и выровняет."""
    update = plan_group_update(listing, config, threshold)
    if update.empty:
        report.unchanged += 1
        return
    try:
        if update.base_changed:
            call_with_retry(v2.patch_offer, listing.ggsell_offer_id, {"price": float(update.base_price)},
                            sleep=sleep, retry_network=True)
        if update.variants:
            call_with_retry(v2.create_or_update_variants, listing.ggsell_offer_id, listing.ggsell_option_id,
                            update.variants, sleep=sleep, retry_network=True)
    except GGSellError as e:
        if e.status_code == 404:
            listing.status = ListingStatus.ARCHIVED
            report.deleted_on_ggsell += 1
        else:
            report.errors[listing.ggsell_offer_id] = f"{e.status_code}: {e.payload}"
        session.commit()
        return
    apply_group_update(listing, update)
    session.commit()
    report.patched += 1
    report.variants_patched += len(update.variants)
    report.margin_fixes.extend(update.margin_fixes)


def _variant_total(base_price: Decimal, variant: dict[str, Any]) -> Decimal:
    """Итоговая цена варианта на витрине: база ± надбавка (fixed или percent)."""
    amount = Decimal(str(variant.get("price") or 0))
    if variant.get("discount_type") == "percent":
        amount = base_price * amount / 100
    sign = -1 if variant.get("impact_type") == "decrease" else 1
    return (base_price + sign * amount).quantize(Decimal("0.01"))


def sync_group_variants(
    session: Session, v2: GGSellV2Client, sleep: Callable[[float], None] = time.sleep
) -> dict[str, int]:
    """Фактические надбавки и статусы вариантов групповых лотов — с витрины, как
    цены отдельных лотов в sync_listings: изменённую вручную надбавку прайсер
    вернёт к расчётной, а после частичного сбоя отправки цены выровняются."""
    changed = hidden = errors = 0
    group_listings = session.scalars(select(Listing).where(
        Listing.is_group.is_(True), Listing.status != ListingStatus.ARCHIVED, Listing.ggsell_option_id.is_not(None),
    )).all()
    for listing in group_listings:
        try:
            response = call_with_retry(v2.list_offer_options, listing.ggsell_offer_id, sleep=sleep, retry_network=True)
        except GGSellError as e:
            errors += 1
            logger.warning("Варианты лота %s не получены: %s", listing.ggsell_offer_id, e)
            continue
        option = next((o for o in options_list(response) if o.get("id") == listing.ggsell_option_id), None)
        on_ggsell = {v["id"]: v for v in (option or {}).get("variants") or []}
        for variant in listing.variants:
            if variant.ggsell_variant_id is None:
                continue
            live = on_ggsell.get(variant.ggsell_variant_id)
            if live is None:
                if variant.status != "hidden":
                    logger.warning("Лот %s: вариант %s удалён на GGSell", listing.ggsell_offer_id, variant.ggsell_variant_id)
                    variant.status = "hidden"
                    hidden += 1
                continue
            status = "active" if live.get("status", "active") == "active" else "hidden"
            total = _variant_total(listing.price_rub, live)
            if total != variant.price_rub or status != variant.status:
                logger.info("Лот %s, %s: на GGSell %s ₽ (%s), у нас %s ₽ — беру с витрины", listing.ggsell_offer_id,
                            variant.position.name, total, status, variant.price_rub)
                variant.price_rub, variant.status = total, status
                changed += 1
    session.commit()
    return {"variants_changed_on_ggsell": changed, "variants_hidden": hidden, "errors": errors}
