"""FastAPI-приложение: вебхуки от GGSell + health-check.

Запуск для локальной разработки:
    uvicorn app.api.main:app --reload --port 8000
"""

from __future__ import annotations

import logging

from fastapi import FastAPI

from app.api.webhooks import router as webhooks_router

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

app = FastAPI(title="ggsell-autosync")
app.include_router(webhooks_router)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}
