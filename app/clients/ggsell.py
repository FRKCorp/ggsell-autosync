"""
Клиенты для GGSell Seller API — обе версии сразу, т.к. они закрывают разные
части задачи и не взаимозаменяемы (см. docs/architecture-notes.md, раздел 3.1):

  GGSellV2Client — статичный API-ключ. Каталог: Category/Options/Products/Offers.
                   Используется для автозалива черновиков и синхронизации цен.

  GGSellV1Client — логин по подписи (seller_id + timestamp + SHA256), токен с
                   ограниченным сроком жизни. Заказы и чат: последние продажи,
                   детали заказа, отправка сообщения покупателю.

Важно (зафиксировано в architecture-notes.md, 3.6): у GGSell нет API-вызова
для "подтверждения/закрытия" заказа при delivery=manual — этот шаг закрывать
не нужно, happy path заканчивается отправкой сообщения в чат.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

BASE_URL = "https://seller.ggsel.com"


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
        # Схема auth в доках: "Security Scheme Type: apiKey, Header parameter
        # name: Authorization". Не уточнено, нужен ли префикс вроде "Bearer" —
        # если первый же вызов вернёт 401, попробовать добавить префикс.
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
        """options: список вида {type, status, title_ru, title_en, is_required, ...}
        (см. option_object / option_regular_* примеры в схеме). Для radio_button
        с номиналами каждый элемент options дополнительно несёт "variants": [...].
        """
        return self._request(
            "POST",
            f"/api_sellers/v2/offers/{offer_id}/options",
            json_body={"options": options},
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
        # TODO: узнать точное имя параметра размера страницы — "per_page"
        # отклонён бэкендом как unpermitted parameter (422). Пока передаём
        # только "page"; extra_params — для эксперимента после того, как
        # узнаем реальное имя (limit? page_size?) из Swagger.
        return self._request("GET", "/api_sellers/v2/offers", params={"page": page, **extra_params})

    def create_offer(self, payload: dict[str, Any]) -> Any:
        """payload — create_offer_request_object целиком: title_ru/en,
        description_ru/en, instructions_ru/en, cover_image_ru (base64!),
        price, category_id, delivery ("auto"/"manual"), и т.д.

        Для нашего сценария: delivery="manual", is_autoselling=False,
        notification_settings={"type": "url", "url": <наш вебхук>,
        "http_method": "POST", "is_disabled": False, "is_default": False}.

        TODO подтвердить на первом реальном вызове: создаётся ли оффер сразу
        в status="draft" (нужно для требования клиента "заливать в черновик").
        """
        return self._request("POST", "/api_sellers/v2/offers", json_body=payload)

    def get_offer(self, offer_id: int) -> Any:
        return self._request("GET", f"/api_sellers/v2/offers/{offer_id}")

    def patch_offer(self, offer_id: int, payload: dict[str, Any]) -> Any:
        """TODO подтвердить на первом реальном вызове: принимает ли PATCH
        частичный объект (например, только {"price": ...}) или требует
        пересылать весь update_offer_request_object целиком — от этого
        зависит, как sync/ будет собирать запрос при обновлении цены.
        """
        return self._request("PATCH", f"/api_sellers/v2/offers/{offer_id}", json_body=payload)

    def batch_activate_offers(self, offer_ids: list[int]) -> Any:
        return self._request(
            "POST", "/api_sellers/v2/offers/batch-activate", json_body={"offer_ids": offer_ids}
        )

    def batch_pause_offers(self, offer_ids: list[int]) -> Any:
        return self._request(
            "POST", "/api_sellers/v2/offers/batch-pause", json_body={"offer_ids": offer_ids}
        )

    def batch_delete_offers(self, offer_ids: list[int]) -> Any:
        return self._request(
            "POST", "/api_sellers/v2/offers/batch-delete", json_body={"offer_ids": offer_ids}
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
    # Account
    # ------------------------------------------------------------------

    def get_balance(self) -> Any:
        # TODO: путь подтверждённо неверен (404 на живом тесте). Проверить точный
        # путь в Swagger V1, раздел Account.
        return self._request("GET", "/api_sellers/api/seller-balance-info")

    def list_categories(self) -> Any:
        return self._request("GET", "/api_sellers/api/return-all-categories")

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
    # Chats — механизм выдачи товара при delivery="manual"
    # ------------------------------------------------------------------

    def create_message(self, chat_id: int, message: str) -> Any:
        """chat_id — это id_i из chats_object, не id заказа напрямую.
        Если у заказа нет готового id_i — сначала найти чат через list_chats.
        """
        return self._request(
            "POST",
            "/api_sellers/api/debates/v2",
            params={"id_i": chat_id},
            json_body={"message": message},
        )

    def list_messages(self, chat_id: int) -> Any:
        return self._request(
            "GET", "/api_sellers/api/debates/messages", params={"id_i": chat_id}
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
        """Список чатов. Каждый элемент: {id_i (это CHAT id, не invoice_id
        заказа — совпадение имени поля чисто случайное на стороне GGSell), email,
        product (offer_id), last_message, cnt_msg, cnt_new}. Фильтруй по email
        покупателя (из get_order_info.content.buyer_info.email) и сверяй
        product == item_id заказа, чтобы найти нужный чат для create_message.
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

    # ------------------------------------------------------------------
    # Products — bulk price update (кандидат на замену поштучных V2 PATCH)
    # ------------------------------------------------------------------

    def bulk_update_prices(self, updates: list[dict[str, Any]]) -> Any:
        """updates — TODO: точная форма элемента не подтверждена (product_id +
        price? variant_id?) — проверить в Swagger тело запроса перед
        использованием в проде. Заложено на будущее сравнение с V2 patch_offer.
        """
        return self._request(
            "POST", "/api_sellers/api/product/edit/prices", json_body={"products": updates}
        )


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return {"ok": False, "error": response.text or "empty_response"}
