"""Массовый импорт позиций топапов из data/selected_topup_categories.json
в БД — вся линейка номиналов по каждой подтверждённой клиентом категории.

Запуск: python scripts/bulk_import_topups.py
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient, FazerCardsError  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.sync.fz_catalog import import_all_topup_offers  # noqa: E402


def main() -> None:
    load_dotenv()
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "selected_topup_categories.json",
    )
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    items = config["items"]
    print(f"Категорий к импорту: {len(items)}")

    session = SessionLocal()
    total_created = 0
    total_updated = 0
    total_errors = 0

    try:
        with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
            for entry in items:
                category_id = entry["category_id"]
                region_label = entry.get("region_label")
                try:
                    results = import_all_topup_offers(
                        session, client, category_id, region_label=region_label
                    )
                    created = sum(1 for r in results if r.created)
                    updated = len(results) - created
                    total_created += created
                    total_updated += updated
                    print(
                        f"  ✅ {category_id} ({region_label or '—'}): "
                        f"{len(results)} офферов (создано={created}, обновлено={updated})"
                    )
                except FazerCardsError as e:
                    total_errors += 1
                    print(f"  ❌ {category_id}: FazerCards error {e.status_code}: {e.error}")

        session.commit()
        print()
        print(
            f"Готово. Всего создано={total_created}, обновлено={total_updated}, "
            f"ошибок категорий={total_errors}"
        )
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
