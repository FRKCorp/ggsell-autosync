"""Точка входа: запускает планировщик синхронизации цен.

Запуск: python -m app.sync.main
Остановка: Ctrl+C.
"""

from __future__ import annotations

import logging

from app.sync.scheduler import build_scheduler

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
# httpx на INFO пишет полный URL запроса — у GGSell V1 в нём токен (?token=...).
logging.getLogger("httpx").setLevel(logging.WARNING)
# «Running job … executed successfully» на каждый запуск — опрос FZ раз в 15 с
# забил бы лог; ошибки джоб (ERROR) и пропуски (WARNING, apscheduler.scheduler) видны.
logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def main() -> None:
    scheduler = build_scheduler()
    logger.info("Планировщик запущен. Ctrl+C для остановки.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Остановка планировщика...")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
