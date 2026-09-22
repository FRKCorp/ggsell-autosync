"""
Клиент для FazerCards Reseller API v2.
Документация: https://reseller.fazercards.com/ru/docs

Особенности, учтённые в реализации:
- Аутентификация: заголовок X-API-Key.
- Идемпотентность: все create-заказ эндпоинты поддерживают Idempotency-Key.
- Rate limits по категориям (каждая — свой sliding window, ключ — API key):
    catalog_read   : 120 / min  (GET /topups, /giftcards, /steam-gifts/games, ...)
    order_create   : 60  / min  (POST .../order)
    order_status   : 120 / min  (GET /orders/:id, поллинг)
    account        : 30  / min  (GET /me, /balance, /transactions, /subscription)
    payments       : 15  / min  (POST /payments/*)
    other          : 120 / min  (всё остальное)
- На 429 сервер отдаёт Retry-After (секунды) — используем его как нижнюю границу
  ожидания + случайный джиттер ±15%, чтобы воркеры не выстраивались синхронно.
"""

from __future__ import annotations

import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

import httpx

BASE_URL = "https://api.fzr.cards/api/v2"


class RateCategory(str, Enum):
    CATALOG_READ = "catalog_read"
    ORDER_CREATE = "order_create"
    ORDER_STATUS = "order_status"
    ACCOUNT = "account"
    PAYMENTS = "payments"
    OTHER = "other"


_CATEGORY_LIMITS = {
    RateCategory.CATALOG_READ: 120,
    RateCategory.ORDER_CREATE: 60,
    RateCategory.ORDER_STATUS: 120,
    RateCategory.ACCOUNT: 30,
    RateCategory.PAYMENTS: 15,
    RateCategory.OTHER: 120,
}


class FazerCardsError(Exception):
    """Базовая ошибка API. Содержит код ответа и тело."""

    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self.payload = payload
        self.error = payload.get("error", "unknown_error")
        self.code = payload.get("code")
        super().__init__(f"FazerCards API error {status_code}: {self.error} ({self.code})")


class FazerCardsRateLimitError(FazerCardsError):
    """429 — превышен лимит категории. retry_after в секундах из заголовка."""

    def __init__(self, status_code: int, payload: dict[str, Any], retry_after: float):
        super().__init__(status_code, payload)
        self.retry_after = retry_after


@dataclass
class _SlidingWindow:
    """Простой sliding-window счётчик запросов в памяти процесса.

    Для распределённого воркера (несколько процессов/подов) этого счётчика
    недостаточно — тогда лимит нужно синхронизировать через Redis. Для одного
    процесса-синхронизатора этого достаточно и не требует внешних зависимостей.
    """

    limit_per_min: int
    _timestamps: list[float] = field(default_factory=list)

    def wait_if_needed(self) -> None:
        now = time.monotonic()
        window_start = now - 60
        self._timestamps = [t for t in self._timestamps if t > window_start]
        if len(self._timestamps) >= self.limit_per_min:
            sleep_for = self._timestamps[0] + 60 - now
            if sleep_for > 0:
                time.sleep(sleep_for)
        self._timestamps.append(time.monotonic())


class FazerCardsClient:
    def __init__(
        self,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self._api_key = api_key
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._max_retries = max_retries
        self._windows = {cat: _SlidingWindow(limit) for cat, limit in _CATEGORY_LIMITS.items()}

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "FazerCardsClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Внутренний транспорт
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        category: RateCategory,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        headers = {"X-API-Key": self._api_key}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        attempt = 0
        while True:
            self._windows[category].wait_if_needed()
            response = self._client.request(
                method, path, params=params, json=json_body, headers=headers
            )

            if response.status_code == 429:
                attempt += 1
                if attempt > self._max_retries:
                    payload = _safe_json(response)
                    raise FazerCardsRateLimitError(429, payload, retry_after=0)
                retry_after = float(response.headers.get("Retry-After", "1"))
                jitter = retry_after * random.uniform(-0.15, 0.15)
                time.sleep(max(0.0, retry_after + jitter))
                continue

            payload = _safe_json(response)
            if response.status_code >= 400:
                raise FazerCardsError(response.status_code, payload)
            return payload

    # ------------------------------------------------------------------
    # Аккаунт
    # ------------------------------------------------------------------

    def get_me(self) -> dict[str, Any]:
        return self._request("GET", "/me", RateCategory.ACCOUNT)

    def get_balance(self) -> dict[str, Any]:
        return self._request("GET", "/balance", RateCategory.ACCOUNT)

    # ------------------------------------------------------------------
    # Игровые пополнения (topup)
    # ------------------------------------------------------------------

    def list_topup_categories(self, limit: int = 50, cursor: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/topups", RateCategory.CATALOG_READ, params=params)

    def get_topup_offers(self, category_id: str) -> dict[str, Any]:
        return self._request(
            "GET", "/topups/offers", RateCategory.CATALOG_READ, params={"category_id": category_id}
        )

    def order_topup(
        self,
        category_id: str,
        offer_id: str,
        fields: dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/topups/order",
            RateCategory.ORDER_CREATE,
            json_body={"category_id": category_id, "offer_id": offer_id, "fields": fields},
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    # ------------------------------------------------------------------
    # Подарочные карты
    # ------------------------------------------------------------------

    def list_giftcard_categories(self, limit: int = 50, cursor: Optional[str] = None) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/giftcards", RateCategory.CATALOG_READ, params=params)

    def get_giftcard_offers(self, category_id: str) -> dict[str, Any]:
        return self._request(
            "GET", "/giftcards/cards", RateCategory.CATALOG_READ, params={"category_id": category_id}
        )

    def order_giftcard(
        self,
        category_id: str,
        card_id: str,
        quantity: int = 1,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/giftcards/order",
            RateCategory.ORDER_CREATE,
            json_body={"category_id": category_id, "card_id": card_id, "quantity": quantity},
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    # ------------------------------------------------------------------
    # Steam gifts
    # ------------------------------------------------------------------

    def list_steam_gift_games(self, limit: int = 100) -> dict[str, Any]:
        return self._request(
            "GET", "/steam-gifts/games", RateCategory.CATALOG_READ, params={"limit": limit}
        )

    def get_steam_gift_offers(self, appid: int) -> dict[str, Any]:
        return self._request("GET", f"/steam-gifts/games/{appid}", RateCategory.CATALOG_READ)

    def order_steam_gift(
        self,
        invite_url: str,
        sub_id: int,
        app_id: int,
        region: str,
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/steam-gifts/order",
            RateCategory.ORDER_CREATE,
            json_body={
                "invite_url": invite_url,
                "sub_id": sub_id,
                "app_id": app_id,
                "region": region,
            },
            idempotency_key=idempotency_key or str(uuid.uuid4()),
        )

    # ------------------------------------------------------------------
    # Заказы (общие)
    # ------------------------------------------------------------------

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/orders/{order_id}", RateCategory.ORDER_STATUS)

    def list_orders(self, page: int = 1, limit: int = 20) -> dict[str, Any]:
        return self._request(
            "GET", "/orders", RateCategory.ORDER_STATUS, params={"page": page, "limit": limit}
        )


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        return response.json()
    except ValueError:
        return {"ok": False, "error": response.text or "empty_response"}