"""Задачи для APScheduler — обёртки над app.sync.fz_catalog с логированием
и управлением сессией/клиентом (сессия и клиент живут ровно один запуск
задачи, не переиспользуются между вызовами)."""

from __future__ import annotations

import logging
import os

from app import settings
from app.clients.fazercards import FazerCardsClient
from app.clients.ggsell import GGSellV2Client
from app.db import SessionLocal
from app.orders.notifications import notify_admin
from app.pricing.exchange_rate import refresh_rate
from app.pricing.listing_price import load_pricing_config
from app.sync.fz_catalog import refresh_all_positions
from app.sync.showcase import check_availability, fetch_offers, push_prices, sync_listings

logger = logging.getLogger(__name__)


def refresh_prices_job() -> None:
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    session = SessionLocal()
    updated = 0
    changed = 0
    errors = 0
    try:
        # Курс (ЦБ + надбавка, roadmap 3.4) — перед ценами; ЦБ недоступен —
        # остаётся последний сохранённый, синхронизация не падает.
        refresh_rate(session)
        session.commit()

        with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
            results = refresh_all_positions(session, client)

        for position, result, error in results:
            if error:
                errors += 1
                logger.warning(
                    "Ошибка обновления позиции id=%s (%s): %s",
                    position.id, position.external_id, error,
                )
                continue
            updated += 1
            if result.price_changed:
                changed += 1
                logger.info(
                    "Цена изменилась: %s (%s) %s -> %s USD",
                    position.name, position.external_id, result.old_price, result.new_price,
                )

        session.commit()
        logger.info(
            "Цены FZ обновлены: всего=%d, обновлено=%d, изменилось=%d, ошибок=%d",
            len(results), updated, changed, errors,
        )

        availability = check_availability(session, results)
        logger.info("Доступность у FZ: %s", availability)

        sync_showcase(session)
    except Exception:
        session.rollback()
        logger.exception("Синхронизация цен упала с необработанной ошибкой")
        raise
    finally:
        session.close()


def sync_showcase(session) -> None:
    """Витрина GGSell (этап 4): статусы и цены лотов с витрины, затем наши цены — если прайсер
    включён (выключатель в панели, roadmap 6.8). Сбой GGSell не роняет
    синхронизацию: цены FZ уже сохранены, следующий запуск дошлёт."""
    api_key = os.getenv("GGSELL_API_KEY")
    if not api_key:
        logger.warning("GGSELL_API_KEY не задан — витрину GGSell не синхронизирую")
        return
    try:
        with GGSellV2Client(
            api_key=api_key,
            base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
            timeout=30,
        ) as v2:
            synced = sync_listings(session, fetch_offers(v2))
            logger.info("Лоты GGSell: %s", synced)

            if not settings.pricer_enabled(session):
                logger.info("Прайсер выключен — цены на GGSell не отправляю")
                return
            report = push_prices(session, v2, load_pricing_config(session))
        settings.touch(session, settings.PRICES_UPDATED_AT)
        session.commit()
        logger.info("Цены на GGSell: %s", report.summary())
    except Exception as e:
        session.rollback()
        logger.exception("Синхронизация витрины GGSell упала")
        notify_admin(f"❗ Синхронизация цен на GGSell упала: {e!r}. Цены FZ обновлены, повтор — со следующим запуском.")
