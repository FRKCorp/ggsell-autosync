"""FastAPI-приложение: вебхуки от GGSell + health-check.

Запуск для локальной разработки:
    uvicorn app.api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app import settings
from app.api.webhooks import router as webhooks_router
from app.db import SessionLocal
from app.ops.heartbeat import age_seconds, heartbeat_key, mark_started

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
# httpx на INFO пишет полный URL запроса — у GGSell V1 в нём токен (?token=...).
logging.getLogger("httpx").setLevel(logging.WARNING)

SCHEDULER_STALE_SECONDS = 10 * 60


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Отметка запуска — сторож сообщит о перезапуске app.
    try:
        mark_started("app")
    except Exception:  # noqa: BLE001 — без БД вебхуки всё равно не обработать, но /health отвечает
        logging.getLogger(__name__).exception("Отметка запуска app не записана")
    yield


app = FastAPI(title="ggsell-autosync", lifespan=lifespan)
app.include_router(webhooks_router)


@app.get("/health")
async def health() -> dict:
    """Жив ли процесс (проверка после деплоя, сторож)."""
    return {"ok": True}


@app.get("/health/full")
def health_full() -> JSONResponse:
    """Для внешнего мониторинга (UptimeRobot и т.п., INSTRUCTIONS.md): 200 —
    БД доступна и scheduler жив; 503 — что-то из этого не так. Внешний
    мониторинг ловит то, о чём изнутри сообщить некому: упал сервер целиком
    или сам scheduler (в котором работает сторож)."""
    report: dict = {"ok": True, "checked_at": datetime.now(timezone.utc).isoformat()}
    session = SessionLocal()
    try:
        session.execute(text("SELECT 1"))
        report["db"] = "ok"
        ages = {s: age_seconds(session, heartbeat_key(s)) for s in ("scheduler", "bot")}
        report["heartbeat_age_seconds"] = {s: (round(a) if a is not None else None) for s, a in ages.items()}
        report["last_sync_finished_at"] = settings.get(session, settings.SYNC_FINISHED_AT)
        if ages["scheduler"] is None or ages["scheduler"] > SCHEDULER_STALE_SECONDS:
            report["ok"] = False
            report["problem"] = "scheduler не подаёт признаков жизни"
    except Exception as e:  # noqa: BLE001
        report.update(ok=False, db=f"error: {type(e).__name__}", problem="БД недоступна")
    finally:
        session.close()
    return JSONResponse(report, status_code=200 if report["ok"] else 503)
