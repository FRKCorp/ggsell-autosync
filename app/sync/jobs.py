"""Задачи для APScheduler — обёртки над app.sync.fz_catalog с логированием
и управлением сессией/клиентом (сессия и клиент живут ровно один запуск
задачи, не переиспользуются между вызовами)."""

from __future__ import annotations

import logging
import os

from app import settings
from app.clients.fazercards import FazerCardsClient
from app.db import SessionLocal
from app.pricing.exchange_rate import refresh_rate
from app.sync.fz_catalog import refresh_all_positions

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

        settings.touch(session, settings.PRICES_UPDATED_AT)
        session.commit()
        logger.info(
            "Синхронизация цен завершена: всего=%d, обновлено=%d, изменилось=%d, ошибок=%d",
            len(results), updated, changed, errors,
        )
    except Exception:
        session.rollback()
        logger.exception("Синхронизация цен упала с необработанной ошибкой")
        raise
    finally:
        session.close()
