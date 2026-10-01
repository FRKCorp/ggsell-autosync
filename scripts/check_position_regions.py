"""Проверка регионов позиций (roadmap 1.9): у каждой позиции из конфигов есть
регион, он виден в названии, вариант (если задан в конфиге) сохранён.

Запуск: python scripts/check_position_regions.py
На проде: docker compose exec app python scripts/check_position_regions.py
Код выхода 1 — есть проблемы (список печатается).

Позиции, которые последний импорт не обновил (у FZ их больше нет — номинал
или категория пропали), проблемой не считаются: регион им проставить не из
чего, а пропавшие у поставщика товары разбираются вручную (roadmap 4.3).
Они печатаются отдельным списком.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter
from datetime import timedelta

from dotenv import load_dotenv
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal  # noqa: E402
from app.models.position import Position, SourceType  # noqa: E402


def main() -> None:
    load_dotenv()
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    config = {}
    for name in ("selected_topup_categories.json", "selected_giftcard_categories.json"):
        with open(os.path.join(data_dir, name), encoding="utf-8") as f:
            for item in json.load(f)["items"]:
                config[item["category_id"]] = item

    session = SessionLocal()
    positions = session.scalars(
        select(Position).where(Position.source_type.in_([SourceType.TOPUP, SourceType.GIFTCARD]))
    ).all()

    # Импорт обновляет updated_at у всех позиций, которые FZ ещё отдаёт.
    newest = max((p.updated_at for p in positions), default=None)
    stale_before = newest - timedelta(hours=1) if newest else None

    problems = []
    stale = []
    regions = Counter()
    variants = Counter()
    for p in positions:
        item = config.get(p.fz_category_id)
        if item is None:
            continue  # тестовые позиции вне конфигов
        regions[p.region] += 1
        if p.variant_label:
            variants[f"{p.fz_category_id}: {p.variant_label}"] += 1
        if not p.region and stale_before and p.updated_at < stale_before:
            stale.append(f"{p.external_id} (последнее обновление {p.updated_at:%d.%m %H:%M})")
            continue
        if not p.region:
            problems.append(f"{p.external_id}: нет региона")
        elif not re.search(rf"(?<!\w){re.escape(p.region)}(?!\w)", p.name, re.IGNORECASE):
            problems.append(f"{p.external_id}: регион {p.region!r} не виден в названии {p.name!r}")
        if item.get("variant_label") and p.variant_label != item["variant_label"]:
            problems.append(f"{p.external_id}: вариант {p.variant_label!r}, в конфиге {item['variant_label']!r}")
    session.close()

    print(f"Позиций проверено: {sum(regions.values())}")
    print(f"Регионы: {dict(regions.most_common())}")
    print(f"Варианты: {dict(variants)}")
    if stale:
        print(f"\nПропали у FZ (импорт их не обновил), без региона — {len(stale)}:")
        print("\n".join(f"  {s}" for s in stale))
    if problems:
        print(f"\n❌ Проблем: {len(problems)}")
        print("\n".join(problems[:50]))
        sys.exit(1)
    print("\n✅ У всех позиций есть регион, и он виден в названии.")


if __name__ == "__main__":
    main()
