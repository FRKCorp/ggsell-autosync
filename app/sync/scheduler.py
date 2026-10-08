"""Настройка APScheduler: периодический запуск refresh_prices_job.

Интервал берётся из CATALOG_SYNC_INTERVAL_HOURS (.env), сейчас 12 часов —
как согласовано с клиентом. Первый запуск происходит сразу при старте
планировщика (next_run_time=now), а не через 12 часов ожидания — это
осознанное решение для удобства разработки и деплоя; если для прода нужно
не запускать сразу при рестарте сервиса, эта строка легко убирается.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger
from dotenv import load_dotenv

from app import settings
from app.clients.telegram import admin_chat_ids, bot_token
from app.db import SessionLocal
from app.ops.heartbeat import beat, mark_started
from app.ops.watchdog import Environment, run_watchdog
from app.orders.jobs import poll_fz_orders_job, sweep_missed_orders_job
from app.orders.notifications import notify_admin
from app.sync.jobs import is_refresh_running, refresh_prices_job

logger = logging.getLogger(__name__)


def build_scheduler() -> BlockingScheduler:
    load_dotenv()
    interval_hours = float(os.getenv("CATALOG_SYNC_INTERVAL_HOURS", "12"))

    scheduler = BlockingScheduler(timezone="UTC")
    scheduler.add_job(
        refresh_prices_job,
        trigger=IntervalTrigger(hours=interval_hours),
        id="refresh_prices",
        name="Обновление цен FazerCards -> Position",
        next_run_time=datetime.now(timezone.utc),
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    # Заказы: опрос FZ по ожидающим заказам и страховка
    # от потерянного вебхука. Отдельные потоки — синхронизация цен (~2 мин)
    # их не задерживает.
    scheduler.add_job(
        poll_fz_orders_job,
        # 15 с: на пилоте 05.10 FZ выдал код за 23 с, а опрос раз в минуту
        # отдал его покупателю ещё через 46 с. Лимит FZ order_status — 120/мин.
        trigger=IntervalTrigger(seconds=int(os.getenv("FZ_ORDER_POLL_SECONDS", "15"))),
        id="poll_fz_orders",
        name="Опрос FZ по ожидающим заказам",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        sweep_missed_orders_job,
        trigger=IntervalTrigger(minutes=int(os.getenv("ORDER_SWEEP_MINUTES", "5"))),
        id="sweep_missed_orders",
        name="Страховка от потерянных вебхуков GGSell",
        coalesce=True,
        max_instances=1,
    )
    # «Обновить цены СЕЙЧАС» из бота: бот пишет запрос в БД,
    # здесь он переносит ближайший запуск refresh_prices на «сейчас» — та же
    # джоба, поэтому две синхронизации разом не пойдут (max_instances=1).
    scheduler.add_job(
        lambda: check_price_sync_request(scheduler),
        trigger=IntervalTrigger(seconds=15),
        id="price_sync_requests",
        name="Запрос синхронизации цен из бота",
        coalesce=True,
        max_instances=1,
    )
    # Сторож: сердцебиение scheduler раз в минуту, проверки —
    # раз в 5 минут (app, бот, синхронизация, бэкап, диск, перезапуски).
    scheduler.add_job(
        lambda: beat("scheduler"),
        trigger=IntervalTrigger(minutes=1),
        id="heartbeat",
        name="Сердцебиение scheduler",
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        watchdog_job,
        trigger=IntervalTrigger(minutes=5),
        id="watchdog",
        name="Сторож",
        coalesce=True,
        max_instances=1,
    )
    mark_started("scheduler")
    return scheduler


def watchdog_job() -> None:
    session = SessionLocal()
    try:
        env = Environment(bot_enabled=bool(bot_token() and admin_chat_ids()))
        run_watchdog(session, env, notify_admin)
    except Exception:  # noqa: BLE001 — сторож не должен ронять scheduler
        session.rollback()
        logger.exception("Сторож упал")
    finally:
        session.close()


def check_price_sync_request(scheduler) -> bool:
    if is_refresh_running():
        return False  # запрос подождёт: запуск сейчас APScheduler пропустил бы
    session = SessionLocal()
    try:
        if not settings.take_price_sync_request(session):
            return False
        session.commit()
    finally:
        session.close()
    logger.info("Синхронизация цен запрошена из бота — запускаю")
    scheduler.modify_job("refresh_prices", next_run_time=datetime.now(timezone.utc))
    return True
