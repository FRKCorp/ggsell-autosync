"""
Ручная проверка GGSell API — V2 (каталог) и V1 (заказы/чат).

Запуск из корня проекта:
    python scripts/smoke_test_ggsell.py

Отдельный первый шаг (см. STEP 0) проверяет гипотезу: работает ли единый
API-ключ (с чекбоксами прав в личном кабинете) напрямую и на V1-эндпоинтах,
без старой схемы "логин по подписи -> токен". Если STEP 0 вернёт 200 —
значит GGSellV1Client можно сильно упростить (убрать _login/_sign/токены),
дальнейшие шаги это подтвердят/опровергнут для остальных V1-методов.
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellError, GGSellV1Client, GGSellV2Client  # noqa: E402


def main() -> None:
    load_dotenv()

    base_url = os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")
    api_key = os.getenv("GGSELL_API_KEY")
    v1_seller_id = os.getenv("GGSELL_V1_SELLER_ID")
    v1_api_key = os.getenv("GGSELL_V1_API_KEY")

    if not api_key or api_key == "your_ggsell_api_key":
        print("❌ GGSELL_API_KEY не задан в .env.")
        sys.exit(1)

    print(f"Базовый URL: {base_url}")
    print("=" * 50)

    # ------------------------------------------------------------------
    # STEP 0: проверка гипотезы про единый ключ на V1-эндпоинте напрямую,
    # без логина по подписи. Сырой запрос, в обход GGSellV1Client.
    # ------------------------------------------------------------------
    print("STEP 0 — гипотеза: единый ключ работает и на V1 напрямую")
    print("→ GET /api_sellers/api/seller-last-sales с Authorization: <GGSELL_API_KEY>")
    try:
        with httpx.Client(base_url=base_url, timeout=15.0) as raw_client:
            resp = raw_client.get(
                "/api_sellers/api/seller-last-sales",
                headers={"Authorization": api_key},
            )
        print(f"  status={resp.status_code}")
        if resp.status_code == 200:
            print("  ✅ Гипотеза подтвердилась! Старая схема логин-по-подписи не нужна.")
            print("     Можно упростить GGSellV1Client — убрать _login/_sign/токены.")
        elif resp.status_code == 401:
            print("  ❌ 401 — единый ключ НЕ работает на V1 напрямую.")
            print("     Похоже, старая схема (seller_id + подпись + токен) всё ещё нужна.")
        else:
            print(f"  ⚠️ Неожиданный статус, тело ответа: {_preview(resp)}")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Ошибка запроса: {type(e).__name__}: {e}")
    print()

    # ------------------------------------------------------------------
    # STEP 1: V2 — каталог
    # ------------------------------------------------------------------
    print("STEP 1 — V2 (каталог)")
    with GGSellV2Client(api_key=api_key, base_url=base_url) as v2:
        _step("GET /api_sellers/v2/categories", v2.list_categories)
        _step("GET /api_sellers/v2/offers (список офферов)", lambda: v2.list_offers(page=1))
    print()

    # ------------------------------------------------------------------
    # STEP 2: V1 через GGSellV1Client (старая схема), только если заданы
    # seller_id и отдельный V1-ключ — иначе пропускаем этот шаг.
    # ------------------------------------------------------------------
    print("STEP 2 — V1 через старую схему логина (если задано)")
    if not v1_seller_id or not v1_api_key or v1_api_key == "your_ggsell_v1_api_key":
        print("  ⏭  GGSELL_V1_SELLER_ID / GGSELL_V1_API_KEY не заданы — пропускаю.")
        print("     (Если STEP 0 выше вернул ✅ — это нормально, они и не нужны.)")
    else:
        with GGSellV1Client(
            seller_id=int(v1_seller_id), api_key=v1_api_key, base_url=base_url
        ) as v1:
            _step("GET /seller-last-sales", v1.list_last_sales)

    print("=" * 50)
    print("Готово.")


def _step(label: str, fn) -> None:
    print(f"→ {label}")
    try:
        result = fn()
        print(f"  ✅ ok, тип ответа: {type(result).__name__}")
        _print_preview(result)
    except GGSellError as e:
        print(f"  ❌ Ошибка API: status={e.status_code} payload={e.payload}")
    except Exception as e:  # noqa: BLE001
        print(f"  ❌ Неожиданная ошибка: {type(e).__name__}: {e}")
    print()


def _print_preview(result) -> None:
    if isinstance(result, dict):
        print(f"  ключи ответа: {list(result.keys())}")
    elif isinstance(result, list):
        print(f"  элементов в списке: {len(result)}, пример: {result[:1]}")
    else:
        print(f"  значение: {result!r}"[:200])


def _preview(resp: httpx.Response) -> str:
    try:
        return str(resp.json())[:300]
    except ValueError:
        return resp.text[:300]


if __name__ == "__main__":
    main()
