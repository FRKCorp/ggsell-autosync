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

from app.orders.jobs import poll_fz_orders_job, sweep_missed_orders_job
from app.sync.jobs import refresh_prices_job

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
    # Заказы (roadmap 5.2, 5.7): опрос FZ по ожидающим заказам и страховка
    # от потерянного вебхука. Отдельные потоки — синхронизация цен (~2 мин)
    # их не задерживает.
    scheduler.add_job(
        poll_fz_orders_job,
        trigger=IntervalTrigger(seconds=int(os.getenv("FZ_ORDER_POLL_SECONDS", "60"))),
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
    return scheduler
