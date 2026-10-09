"""
Клиенты для GGSell Seller API — обе версии сразу, т.к. они закрывают разные
части задачи и не взаимозаменяемы:

  GGSellV2Client — статичный API-ключ. Каталог: Category/Options/Products/Offers.
                   Используется для автозалива черновиков и синхронизации цен.

  GGSellV1Client — логин по подписи (seller_id + timestamp + SHA256), токен с
                   ограниченным сроком жизни. Заказы и чат: последние продажи,
                   детали заказа, отправка сообщения покупателю.

Важно: у GGSell нет API-вызова для
"подтверждения/закрытия" заказа — happy path заканчивается отправкой
сообщения в чат заказа. Все наши офферы создаются с delivery="auto": сменить
delivery через API нельзя (подтверждено поддержкой GGSell).
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

BASE_URL = "https://seller.ggsel.com"


def v1_api_key() -> Optional[str]:
    """Ключ для входа в API v1 по подписи. Это тот же API-ключ продавца, что и
    для v2 (проверено на живом аккаунте), поэтому GGSELL_V1_API_KEY можно не
    задавать — берётся GGSELL_API_KEY."""
    return os.getenv("GGSELL_V1_API_KEY", "").strip() or os.getenv("GGSELL_API_KEY")


class GGSellError(Exception):
    """Общая ошибка обеих версий API GGSell."""

    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload
        super().__init__(f"GGSell API error {status_code}: {payload}")


# ======================================================================
# V2 — каталог (Category / Options / Products / Offers)
# ======================================================================


class GGSellV2Client:
    def __init__(self, api_key: str, base_url: str = BASE_URL, timeout: float = 15.0):
        self._api_key = api_key
        self._client = httpx.Client(base_url=base_url, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GGSellV2Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> Any:
        # Ключ передаётся в Authorization как есть, без префикса "Bearer" —
        # подтверждено на живых вызовах.
        headers = {"Authorization": self._api_key, "locale": "ru"}
        response = self._client.request(
            method, path, params=params, json=json_body, headers=headers
        )
        if response.status_code >= 400:
            raise GGSellError(response.status_code, _safe_json(response))
        if not response.content:
            return None
        return _safe_json(response)

    # ------------------------------------------------------------------
    # Category
    # ------------------------------------------------------------------

    def list_categories(self, **params: Any) -> Any:
        return self._request("GET", "/api_sellers/v2/categories", params=params or None)

    # ------------------------------------------------------------------
    # Options
    # ------------------------------------------------------------------

    def view_option(self, option_id: int) -> Any:
        return self._request("GET", f"/api_sellers/v2/options/{option_id}")

    def create_or_update_options(self, offer_id: int, options: list[dict[str, Any]]) -> Any:
        """options: список {type, status, title_ru, title_en, comment_ru,
        comment_en, is_required, position} (bulk_options_request_object; с
        "id" — обновление существующей). Вариантов в этом запросе НЕТ — для
        radio_button они создаются отдельно через create_or_update_variants.
        """
        return self._request(
            "POST",
            f"/api_sellers/v2/offers/{offer_id}/options",
            json_body={"options": options},
        )

    def list_offer_options(self, offer_id: int) -> Any:
        """Опции оффера вместе с вариантами (option_list_object) — отсюда
        берём id созданных опций для create_or_update_variants."""
        return self._request("GET", f"/api_sellers/v2/offers/{offer_id}/options")

    def create_or_update_variants(
        self, offer_id: int, option_id: int, variants: list[dict[str, Any]]
    ) -> Any:
        """variants: список {title_ru, title_en, price, discount_type,
        impact_type, is_default, status, position} (bulk_variants_request_object)."""
        return self._request(
            "POST",
            f"/api_sellers/v2/offers/{offer_id}/options/{option_id}/variants",
            json_body={"variants": variants},
        )

    # ------------------------------------------------------------------
    # Products (преднагруженный сток для is_autoselling — не наш основной
    # сценарий, но методы на будущее)
    # ------------------------------------------------------------------

    def list_products(self, offer_id: int) -> Any:
        return self._request("GET", f"/api_sellers/v2/offers/{offer_id}/products")

    def create_products(self, offer_id: int, products: list[dict[str, Any]]) -> Any:
        return self._request(
            "POST", f"/api_sellers/v2/offers/{offer_id}/products", json_body={"products": products}
        )

    # ------------------------------------------------------------------
    # Offers
    # ------------------------------------------------------------------

    def list_offers(self, page: int = 1, **extra_params: Any) -> Any:
        # Размер страницы задать нельзя: "per_page" бэкенд отклоняет как
        # unpermitted parameter (422), вопреки документации. Работает только "page".
        return self._request("GET", "/api_sellers/v2/offers", params={"page": page, **extra_params})

    def create_offer(self, payload: dict[str, Any]) -> Any:
        """payload — create_offer_request_object целиком: title_ru/en,
        description_ru/en, instructions_ru/en, cover_image_ru (base64!),
        price, category_id, delivery ("auto"/"manual"), и т.д.

        Для нашего сценария: delivery="auto" (сменить потом через PATCH нельзя),
        is_autoselling=False, notification_settings={"type": "url",
        "url": <наш вебхук>, "http_method": "POST", "is_disabled": False,
        "is_default": False}.

        Подтверждено: оффер создаётся в status="draft".
        """
        return self._request("POST", "/api_sellers/v2/offers", json_body=payload)

    def get_offer(self, offer_id: int) -> Any:
        return self._request("GET", f"/api_sellers/v2/offers/{offer_id}")

    def patch_offer(self, offer_id: int, payload: dict[str, Any]) -> Any:
        """Частичный PATCH работает: достаточно передать только изменённые
        поля (подтверждено на {"price": ...}). Исключение — delivery: через
        PATCH не меняется вообще.
        """
        return self._request("PATCH", f"/api_sellers/v2/offers/{offer_id}", json_body=payload)

    # Пути batch-методов — через подчёркивание (batch_activate), а не дефис:
    # дефисный вариант отдаёт 404 (проверено 30 сентября). Не больше 100 id
    # за один вызов (batch_offer_ids_request_object).
    BATCH_LIMIT = 100

    def batch_activate_offers(self, offer_ids: list[int]) -> Any:
        return self._batch("batch_activate", offer_ids)

    def batch_pause_offers(self, offer_ids: list[int]) -> Any:
        return self._batch("batch_pause", offer_ids)

    def batch_delete_offers(self, offer_ids: list[int]) -> Any:
        return self._batch("batch_delete", offer_ids)

    def get_async_job_result(self, job_id: str) -> Any:
        """Batch-методы выполняются асинхронно: отвечают {"success": true,
        "job_id": ...}, результат — здесь. batch_delete переводит оффер в
        status="archived" за несколько секунд (проверено 30 сентября)."""
        return self._request("GET", f"/api_sellers/v2/async_job_results/{job_id}")

    def _batch(self, action: str, offer_ids: list[int]) -> Any:
        if len(offer_ids) > self.BATCH_LIMIT:
            raise ValueError(f"{action}: не больше {self.BATCH_LIMIT} офферов за вызов, передано {len(offer_ids)}")
        return self._request(
            "POST", f"/api_sellers/v2/offers/{action}", json_body={"offer_ids": offer_ids}
        )


# ======================================================================
# V1 — заказы и чат (Orders / Chats), плюс bulk-обновление цен
# ======================================================================


@dataclass
class _V1Token:
    value: str
    valid_thru: datetime

    def is_expiring(self, safety_margin_seconds: int = 60) -> bool:
        now = datetime.now(timezone.utc)
        return (self.valid_thru - now).total_seconds() < safety_margin_seconds


class GGSellV1Client:
    def __init__(
        self,
        seller_id: int,
        api_key: str,
        base_url: str = BASE_URL,
        timeout: float = 15.0,
    ):
        self._seller_id = seller_id
        self._api_key = api_key
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._token: Optional[_V1Token] = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "GGSellV1Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Аутентификация
    # ------------------------------------------------------------------

    def _sign(self, timestamp: str) -> str:
        return hashlib.sha256(f"{self._api_key}{timestamp}".encode("utf-8")).hexdigest()

    def _login(self) -> _V1Token:
        timestamp = str(int(time.time()))
        payload = {
            "seller_id": self._seller_id,
            "timestamp": timestamp,
            "sign": self._sign(timestamp),
        }
        response = self._client.post("/api_sellers/api/apilogin", json=payload)
        if response.status_code >= 400:
            raise GGSellError(response.status_code, _safe_json(response))
        data = _safe_json(response)
        # valid_thru — ISO 8601 datetime по схеме (date-time).
        valid_thru = datetime.fromisoformat(data["valid_thru"].replace("Z", "+00:00"))
        return _V1Token(value=data["token"], valid_thru=valid_thru)

    def _ensure_token(self) -> str:
        if self._token is None or self._token.is_expiring():
            self._token = self._login()
        return self._token.value

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Any:
        token = self._ensure_token()
        full_params = {"token": token, **(params or {})}
        response = self._client.request(
            method, path, params=full_params, json=json_body, headers=headers
        )
        if response.status_code >= 400:
            raise GGSellError(response.status_code, _safe_json(response))
        if not response.content:
            return None
        return _safe_json(response)

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def list_last_sales(self) -> Any:
        return self._request("GET", "/api_sellers/api/seller-last-sales")

    def get_order_info(self, invoice_id: int) -> Any:
        return self._request(
            "GET",
            f"/api_sellers/api/purchase/info/{invoice_id}",
            headers={"locale": "ru"},
        )

    def check_unique_code(self, unique_code: str) -> Any:
        """unique_code — НЕ invoice_id, это отдельная сущность (строковый код,
        подтверждающий получение товара покупателем), не id заказа.
        """
        return self._request("GET", f"/api_sellers/api/purchases/unique-code/{unique_code}")

    # ------------------------------------------------------------------
    # Chats — механизм выдачи товара покупателю
    # ------------------------------------------------------------------

    def create_message(self, invoice_id: int, message: str) -> Any:
        """id_i в запросе — это номер заказа (invoice_id), отдельной сущности
        чата нет (подтверждено поддержкой GGSell 27 сентября).
        """
        return self._request(
            "POST",
            "/api_sellers/api/debates/v2",
            params={"id_i": invoice_id},
            json_body={"message": message},
        )

    def list_messages(self, invoice_id: int) -> Any:
        return self._request(
            "GET", "/api_sellers/api/debates/messages", params={"id_i": invoice_id}
        )

    def list_chats(
        self,
        *,
        filter_new: Optional[int] = None,
        email: Optional[str] = None,
        id_ds: Optional[str] = None,
        pagesize: Optional[int] = None,
        page: Optional[int] = None,
    ) -> Any:
        """Список чатов. НЕ ИСПОЛЬЗУЕТСЯ: возвращает записи с id_i: null —
        признанная поддержкой GGSell недоработка метода. Для create_message
        id чата не нужен, там передаётся invoice_id. Оставлен на случай, если
        GGSell починит метод.
        """
        params = {
            "filter_new": filter_new,
            "email": email,
            "id_ds": id_ds,
            "pagesize": pagesize,
            "page": page,
        }
        params = {k: v for k, v in params.items() if v is not None}
        return self._request("GET", "/api_sellers/api/debates/v2/chats", params=params)



def call_with_retry(call, *args, retries: int = 4, sleep=time.sleep, retry_network: bool = False, **kwargs) -> Any:
    """Вызов метода клиента с повторами на 429 и 5xx: GGSell под нагрузкой
    отвечает 504 Gateway Time-out (проверено при выгрузке категорий 30.09).
    Остальные ошибки (4xx) не повторяются.

    retry_network=True — повторять и сбои сети / тайм-ауты (08.10 утренняя
    синхронизация упала на ReadTimeout list_offers). Только для запросов,
    которые безопасно повторить: чтение, PATCH той же цены. Создание оффера
    после тайм-аута повторять нельзя — GGSell мог его уже создать (дубль)."""
    for attempt in range(1, retries + 1):
        try:
            return call(*args, **kwargs)
        except GGSellError as e:
            if not (e.status_code == 429 or e.status_code >= 500) or attempt == retries:
                raise
            sleep(2 * attempt)
        except httpx.TransportError:
            if not retry_network or attempt == retries:
                raise
            sleep(2 * attempt)


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"ok": False, "error": response.text or "empty_response"}
