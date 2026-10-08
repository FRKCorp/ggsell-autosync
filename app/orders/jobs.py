"""Фоновые задачи заказов (запускаются в scheduler, app/sync/scheduler.py).

  - poll_fz_orders_job — раз в минуту: заказы, ждущие FZ (ORDERED_UPSTREAM),
    → GET /orders/{id} → выдача / ручной разбор.
  - sweep_missed_orders_job — раз в несколько минут: страховка от
    потерянного вебхука — свежие продажи из seller-last-sales,
    которых нет у нас, плюс заказы, застрявшие после падения процесса
    (PENDING дольше 2 мин, PROCESSING дольше 10 мин). Повторная обработка
    безопасна: Idempotency-Key у FZ = номер заказа GGSell, FZ вернёт тот же
    заказ без второго списания (ключи живут 7 дней).
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, Optional

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsClient, FazerCardsError
from app.clients.ggsell import GGSellError, GGSellV1Client
from app.db import SessionLocal
from app.models.listing import Listing
from app.models.order import Order, OrderStatus
from app.orders.order_processor import (
    OrderProcessingError,
    poll_upstream_orders,
    process_new_order,
)
from app.pricing.calculator import PricingConfig

logger = logging.getLogger(__name__)

SWEEP_SALES_MAX_AGE = timedelta(hours=24)
STUCK_PENDING_AFTER = timedelta(minutes=2)
STUCK_PROCESSING_AFTER = timedelta(minutes=10)


@contextmanager
def order_clients() -> Iterator[tuple[FazerCardsClient, GGSellV1Client]]:
    with FazerCardsClient(
        api_key=os.getenv("FAZERCARDS_API_KEY"),
        base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2"),
    ) as fz, GGSellV1Client(
        seller_id=int(os.getenv("GGSELL_V1_SELLER_ID")),
        api_key=os.getenv("GGSELL_V1_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
    ) as ggsell_v1:
        yield fz, ggsell_v1


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def find_missed_invoices(
    session: Session, sales: list[dict], now: datetime
) -> list[tuple[str, int]]:
    """(invoice_id, offer_id) свежих продаж наших лотов, которых нет в orders."""
    our_offers = set(session.scalars(select(Listing.ggsell_offer_id)))
    known = set(session.scalars(select(Order.ggsell_invoice_id)))
    missed = []
    for sale in sales:
        invoice_id = str(sale.get("invoice_id"))
        offer_id = (sale.get("product") or {}).get("id")
        if offer_id not in our_offers or invoice_id in known:
            continue
        try:
            sold_at = datetime.fromisoformat(sale["date"])
        except (KeyError, TypeError, ValueError):
            sold_at = now
        if now - _aware(sold_at) <= SWEEP_SALES_MAX_AGE:
            missed.append((invoice_id, offer_id))
    return missed


def find_stuck_orders(session: Session, now: datetime) -> list[Order]:
    """PENDING дольше 2 мин (вебхук записал, но обработка не началась) и
    PROCESSING дольше 10 мин (процесс упал посреди обработки) — последние
    возвращаются в PENDING, чтобы claim_order снова их выдал."""
    stuck = []
    for order in session.scalars(
        select(Order).where(Order.status.in_([OrderStatus.PENDING, OrderStatus.PROCESSING]))
    ):
        age = now - _aware(order.updated_at)
        if order.status == OrderStatus.PENDING and age > STUCK_PENDING_AFTER:
            stuck.append(order)
        elif order.status == OrderStatus.PROCESSING and age > STUCK_PROCESSING_AFTER:
            session.execute(
                update(Order)
                .where(Order.id == order.id, Order.status == OrderStatus.PROCESSING)
                .values(status=OrderStatus.PENDING)
            )
            session.commit()
            logger.warning("Заказ %s завис в обработке — повторяю", order.ggsell_invoice_id)
            stuck.append(order)
    return stuck


def sweep_missed_orders(
    session: Session,
    fz: FazerCardsClient,
    ggsell_v1: GGSellV1Client,
    config: PricingConfig,
    now: Optional[datetime] = None,
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    sales = ggsell_v1.list_last_sales().get("sales") or []
    targets = find_missed_invoices(session, sales, now)
    for invoice_id, _ in targets:
        logger.warning("Заказ %s: вебхук не приходил — обрабатываю по seller-last-sales", invoice_id)
    targets += [(o.ggsell_invoice_id, None) for o in find_stuck_orders(session, now)]

    processed = errors = 0
    for invoice_id, offer_id in targets:
        try:
            process_new_order(session, fz, ggsell_v1, config, invoice_id, expected_offer_id=offer_id)
            processed += 1
        except (OrderProcessingError, GGSellError) as e:
            session.rollback()
            errors += 1
            logger.error("Заказ %s: страховочная обработка не удалась: %s", invoice_id, e)
    return {"found": len(targets), "processed": processed, "errors": errors}


def _is_transient(e: Exception) -> bool:
    if isinstance(e, GGSellError):
        return e.status_code == 429 or e.status_code >= 500
    if isinstance(e, FazerCardsError):
        return e.status_code is not None and (e.status_code == 429 or e.status_code >= 500)
    return isinstance(e, httpx.TransportError)


def _log_job_failure(job: str, e: Exception) -> None:
    """Временный сбой (GGSell 504, сеть) — одной строкой: джоба повторится
    сама через минуту-пять. Остальное — с traceback, это баг."""
    if _is_transient(e):
        logger.warning("%s: временный сбой (%r) — повтор при следующем запуске", job, e)
    else:
        logger.exception("%s упал", job)


def poll_fz_orders_job() -> None:
    session = SessionLocal()
    try:
        with order_clients() as (fz, ggsell_v1):
            stats = poll_upstream_orders(session, fz, ggsell_v1)
        if stats["checked"]:
            logger.info("Опрос FZ по заказам: %s", stats)
    except Exception as e:
        session.rollback()
        _log_job_failure("Опрос FZ по заказам", e)
    finally:
        session.close()


def sweep_missed_orders_job() -> None:
    session = SessionLocal()
    try:
        with order_clients() as (fz, ggsell_v1):
            stats = sweep_missed_orders(session, fz, ggsell_v1, PricingConfig.from_env())
        if stats["found"]:
            logger.info("Страховочная проверка заказов: %s", stats)
    except Exception as e:
        session.rollback()
        _log_job_failure("Страховочная проверка заказов", e)
    finally:
        session.close()
