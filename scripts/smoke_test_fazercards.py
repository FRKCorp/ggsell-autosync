"""
Ручная проверка живого API FazerCards.

Запуск из корня проекта:
    python scripts/smoke_test_fazercards.py

Проверяет по нарастающей: сначала простые аккаунт-эндпоинты (не требуют
доступа к конкретным продуктам), потом чтение каталога. Если каталог
вернёт 403 — это, скорее всего, вопрос тарифа (доступ к продукту не
включён в твой план), а не баг клиента.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient, FazerCardsError 


def main() -> None:
    load_dotenv()
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    if not api_key or api_key == "your_fazercards_api_key_here":
        print("❌ FAZERCARDS_API_KEY не задан в .env (или это ещё значение-плейсхолдер).")
        sys.exit(1)

    print(f"Базовый URL: {base_url}")
    print("—" * 40)

    with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
        _step("GET /me", client.get_me)
        _step("GET /balance", client.get_balance)
        _step("GET /topups (первые 5 категорий)", lambda: client.list_topup_categories(limit=5))
        _step("GET /giftcards (первые 5 категорий)", lambda: client.list_giftcard_categories(limit=5))
        _step("GET /steam-gifts/games (первые 5 игр)", lambda: client.list_steam_gift_games(limit=5))

    print("—" * 40)
    print("Готово. Если все шаги выше со статусом ✅ — клиент реально работает с живым API.")


def _step(label: str, fn) -> None:
    print(f"→ {label}")
    try:
        result = fn()
        print(f"  ✅ ok={result.get('ok')}")
        _print_preview(result)
    except FazerCardsError as e:
        print(f"  ❌ Ошибка API: status={e.status_code} error={e.error} code={e.code}")
    except Exception as e: 
        print(f"  ❌ Неожиданная ошибка: {type(e).__name__}: {e}")
    print()


def _print_preview(result: dict) -> None:
    keys = list(result.keys())
    print(f"  ключи ответа: {keys}")
    if "items" in result and isinstance(result["items"], list):
        print(f"  items[0] (пример): {result['items'][:1]}")
    if "games" in result and isinstance(result["games"], list):
        print(f"  games[0] (пример): {result['games'][:1]}")


if __name__ == "__main__":
    main()
