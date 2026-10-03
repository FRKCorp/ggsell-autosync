"""Задачи для APScheduler — обёртки над app.sync.fz_catalog с логированием
и управлением сессией/клиентом (сессия и клиент живут ровно один запуск
задачи, не переиспользуются между вызовами)."""

from __future__ import annotations

import logging
import os
import threading
from decimal import Decimal

from app import settings
from app.clients.fazercards import FazerCardsClient
from app.clients.ggsell import GGSellV2Client
from app.db import SessionLocal
from app.orders.notifications import notify_admin
from app.pricing.exchange_rate import current_rate, refresh_rate
from app.pricing.listing_price import load_pricing_config
from app.sync.fz_catalog import refresh_all_positions
from app.sync.showcase import check_availability, fetch_offers, fz_errors_alert, push_prices, sync_listings

logger = logging.getLogger(__name__)


# Идёт ли синхронизация сейчас: запрос из бота, пришедший во время неё, нельзя
# превращать в запуск — APScheduler его пропустит (max_instances=1), и смена
# наценки потеряется до следующего запуска через 12 ч (нашли 03.10). Запрос
# ждёт окончания (app/sync/scheduler.py:check_price_sync_request).
_running = threading.Event()


def is_refresh_running() -> bool:
    return _running.is_set()


def refresh_prices_job() -> None:
    _running.set()
    try:
        _refresh_prices()
    finally:
        _running.clear()


def _refresh_prices() -> None:
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    session = SessionLocal()
    updated = 0
    changed = 0
    errors = 0
    try:
        # Запущено из бота (смена наценки / «Обновить сейчас», roadmap 6.8) —
        # админ ждёт новые цены, поэтому порог против колебаний курса не
        # действует: отправляем все изменившиеся цены.
        manual = settings.get_bool(session, settings.PRICE_SYNC_REPORT_PENDING)

        # Курс (ЦБ + надбавка, roadmap 3.4) — перед ценами; ЦБ недоступен —
        # остаётся последний сохранённый, синхронизация не падает.
        rate = refresh_rate(session)
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

        fz_alert = fz_errors_alert(results)
        if fz_alert:
            notify_admin(fz_alert)

        availability = check_availability(session, results)
        logger.info("Доступность у FZ: %s", availability)

        showcase = sync_showcase(session, force=manual)

        if settings.take_price_sync_report(session):
            session.commit()
            notify_admin(
                "🔄 Цены обновлены (запрос из бота)\n"
                f"Курс: 1$ = {current_rate(session).rate} ₽{'' if rate.fresh else ' (ЦБ недоступен — сохранённый)'}\n"
                f"FazerCards: позиций {len(results)}, цена изменилась у {changed}, ошибок {errors}\n"
                f"GGSell: {showcase}"
            )
    except Exception as e:
        session.rollback()
        logger.exception("Синхронизация цен упала с необработанной ошибкой")
        notify_admin(f"❗ Синхронизация цен упала: {e!r}")
        raise
    finally:
        session.close()


def sync_showcase(session, force: bool = False) -> str:
    """Витрина GGSell (этап 4): статусы и цены лотов с витрины, затем наши цены — если прайсер
    включён (выключатель в панели, roadmap 6.8). Сбой GGSell не роняет
    синхронизацию: цены FZ уже сохранены, следующий запуск дошлёт.
    Возвращает итог одной строкой — для отчёта в Telegram."""
    api_key = os.getenv("GGSELL_API_KEY")
    if not api_key:
        logger.warning("GGSELL_API_KEY не задан — витрину GGSell не синхронизирую")
        return "не синхронизирована (нет GGSELL_API_KEY)"
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
                return "прайсер выключен — цены не отправлялись"
            report = push_prices(session, v2, load_pricing_config(session),
                                 threshold_percent=Decimal(0) if force else None)
        settings.touch(session, settings.PRICES_UPDATED_AT)
        session.commit()
        logger.info("Цены на GGSell: %s", report.summary())
        return report.summary()
    except Exception as e:
        session.rollback()
        logger.exception("Синхронизация витрины GGSell упала")
        notify_admin(f"❗ Синхронизация цен на GGSell упала: {e!r}. Цены FZ обновлены, повтор — со следующим запуском.")
        return f"ошибка: {e!r}"
