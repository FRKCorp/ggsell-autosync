"""Выгружает поля, которые FazerCards требует от покупателя при заказе
топапа (Player ID, Server ID, выбор сервера и т.п.), для каждой категории из
data/selected_topup_categories.json — основа для опций GGSell.

FZ отдаёт их в get_topup_offers на уровне категории, поле `fields`:
    [{"key": "player_id", "label": "Player ID", "type": "text"},
     {"key": "server", "label": "Server", "type": "select",
      "options": [{"label": "Asia", "value": "asia"}, ...]}]
`key` — ровно тот ключ, который ждёт order_topup(fields={...}).

Запуск: python scripts/fetch_topup_fields.py
Результат: data/fz_topup_fields.json — {category_id: {"name": ..., "fields": [...]}}
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient, FazerCardsError  # noqa: E402


def main() -> None:
    load_dotenv()
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    with open(os.path.join(data_dir, "selected_topup_categories.json"), encoding="utf-8") as f:
        category_ids = [item["category_id"] for item in json.load(f)["items"]]

    result: dict[str, dict] = {}
    failed: list[str] = []

    with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
        for category_id in category_ids:
            try:
                data = client.get_topup_offers(category_id)
            except FazerCardsError as e:
                failed.append(category_id)
                print(f"  ❌ {category_id}: FazerCards error {e.status_code}: {e.error}")
                continue
            fields = data.get("fields", [])
            result[category_id] = {"name": data.get("name", category_id), "fields": fields}
            summary = ", ".join(
                f"{fld['key']}:{fld['type']}" + (f"[{len(fld['options'])}]" if fld.get("options") else "")
                for fld in fields
            )
            print(f"  ✅ {category_id}: {summary or '— полей нет'}")

    out_path = os.path.join(data_dir, "fz_topup_fields.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    all_fields = [fld for cat in result.values() for fld in cat["fields"]]
    print()
    print(f"✅ Сохранено {len(result)} категорий в {out_path}, ошибок: {len(failed)}")
    print(f"Типы полей: {dict(Counter(fld['type'] for fld in all_fields))}")
    print(f"Ключи полей: {dict(Counter(fld['key'] for fld in all_fields))}")
    if failed:
        print(f"С ошибкой: {', '.join(failed)}")


if __name__ == "__main__":
    main()
