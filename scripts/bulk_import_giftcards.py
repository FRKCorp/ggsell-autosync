"""Массовый импорт позиций giftcards из data/selected_giftcard_categories.json
в БД — все номиналы по каждой подтверждённой клиентом категории.

Запуск: python scripts/bulk_import_giftcards.py
На проде: docker compose exec app python scripts/bulk_import_giftcards.py
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient, FazerCardsError  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.sync.fz_catalog import import_all_giftcard_offers  # noqa: E402


def main() -> None:
    load_dotenv()
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "selected_giftcard_categories.json",
    )
    with open(config_path, encoding="utf-8") as f:
        config = json.load(f)

    items = config["items"]
    print(f"Категорий к импорту: {len(items)}")

    session = SessionLocal()
    total_created = 0
    total_updated = 0
    failed: list[str] = []
    empty: list[str] = []

    try:
        with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
            for entry in items:
                category_id = entry["category_id"]
                region = entry.get("region")
                variant_label = entry.get("variant_label")
                label = ", ".join(x for x in (variant_label, region) if x) or "—"
                try:
                    results = import_all_giftcard_offers(
                        session, client, category_id, region=region, variant_label=variant_label
                    )
                except FazerCardsError as e:
                    failed.append(category_id)
                    print(f"  ❌ {category_id}: FazerCards error {e.status_code}: {e.error}")
                    continue

                if not results:
                    empty.append(category_id)
                    print(f"  ⚠️ {category_id} ({label}): у поставщика 0 карт")
                    continue

                created = sum(1 for r in results if r.created)
                updated = len(results) - created
                total_created += created
                total_updated += updated
                print(
                    f"  ✅ {category_id} ({label}): "
                    f"{len(results)} карт (создано={created}, обновлено={updated})"
                )

        session.commit()
        print()
        print(
            f"Готово. Всего создано={total_created}, обновлено={total_updated}, "
            f"ошибок категорий={len(failed)}, пустых категорий={len(empty)}"
        )
        if failed:
            print(f"С ошибкой: {', '.join(failed)}")
        if empty:
            print(f"Пустые: {', '.join(empty)}")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
