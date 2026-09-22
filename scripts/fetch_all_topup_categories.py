"""Выгружает ПОЛНЫЙ список категорий топапов FazerCards (все страницы) в
JSON-файл — для сверки с русским списком игр от клиента и получения точных
category_id.

Запуск: python scripts/fetch_all_topup_categories.py
Результат: data/fz_topup_categories.json
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient  # noqa: E402


def main() -> None:
    load_dotenv()
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    all_items = []
    cursor = None
    page = 0

    with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
        while True:
            page += 1
            data = client.list_topup_categories(limit=50, cursor=cursor)
            items = data.get("items", [])
            all_items.extend(items)
            print(f"Страница {page}: +{len(items)} (всего {len(all_items)} из {data['meta']['total']})")
            if not data["meta"].get("has_more"):
                break
            cursor = data["meta"]["next_cursor"]

    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "fz_topup_categories.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(all_items, f, ensure_ascii=False, indent=2)

    print(f"\n✅ Сохранено {len(all_items)} категорий в {out_path}")


if __name__ == "__main__":
    main()
