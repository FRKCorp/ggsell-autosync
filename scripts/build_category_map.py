"""Подбирает категорию GGSell для каждой позиции в БД по правилам
data/ggsell_category_rules.json и дереву data/ggsell_category_tree.json
(roadmap 3.5, подбор категорий).

Запуск: python scripts/build_category_map.py
Результат:
  data/ggsell_category_map.json — {external_id: {category_id, tree, fee,
      payment_fee, via_fallback, review, note}} для всех подобранных позиций;
  data/ggsell_category_report.md — что не подобралось, что подобралось через
      «Другое количество»/«Другие страны», что помечено на проверку.
Недостающих детей дерева дозапрашивает у GGSell и дописывает в дерево.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict

from dotenv import load_dotenv
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellV2Client  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.position import Position  # noqa: E402
from app.offers.categories import (  # noqa: E402
    CategoryNotResolved,
    CategoryTree,
    resolve_category,
    rules_for,
)


def main() -> None:
    load_dotenv()
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

    def load(name):
        with open(os.path.join(data_dir, name), encoding="utf-8") as f:
            return json.load(f)

    rules = load("ggsell_category_rules.json")
    tree_data = load("ggsell_category_tree.json")
    selected_categories = set()
    for config in ("selected_topup_categories.json", "selected_giftcard_categories.json"):
        for item in load(config)["items"]:
            selected_categories.add(item["category_id"])

    fetched: dict[int, list] = {}
    with GGSellV2Client(
        api_key=os.getenv("GGSELL_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
    ) as v2:

        def fetch_children(node_id: int) -> list:
            response = v2.list_categories(parent_id=node_id)
            children = response.get("data", response) if isinstance(response, dict) else response
            fetched[node_id] = children
            return children

        tree = CategoryTree(tree_data, fetch_children=fetch_children)

        session = SessionLocal()
        positions = session.scalars(select(Position).order_by(Position.fz_category_id, Position.id)).all()

        result: dict[str, dict] = {}
        unresolved: dict[str, list[str]] = defaultdict(list)
        fallback: dict[str, list[str]] = defaultdict(list)
        review: dict[str, list[str]] = defaultdict(list)
        skipped = []

        for p in positions:
            if p.fz_category_id not in selected_categories:
                skipped.append(p.external_id)  # тестовые позиции вне конфигов
                continue
            category_rules = rules_for(rules, p.fz_category_id)
            offer_name = p.raw_payload.get("name", p.name)
            if category_rules is None:
                unresolved[f"{p.fz_category_id}: нет правил"].append(offer_name)
                continue
            try:
                # Регион — у самой позиции (app/regions.py, roadmap 1.9): у CapCut
                # он свой у каждого номинала, у остальных — регион категории.
                choice = resolve_category(tree, category_rules, offer_name, p.region)
            except CategoryNotResolved as e:
                unresolved[f"{p.fz_category_id}: {e}"].append(offer_name)
                continue
            result[p.external_id] = {
                "category_id": choice.category_id,
                "tree": choice.tree,
                "fee": choice.fee,
                "payment_fee": choice.payment_fee,
                "via_fallback": choice.via_fallback,
                "review": choice.review,
                "note": choice.note,
            }
            if choice.via_fallback:
                fallback[f"{p.fz_category_id} → {choice.tree}"].append(offer_name)
            if choice.review:
                review[f"{p.fz_category_id} → {choice.tree} ({choice.note})"].append(offer_name)
        session.close()

    with open(os.path.join(data_dir, "ggsell_category_map.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)

    if fetched:
        # Дописываем дозагруженных детей в дерево — следующий прогон без API.
        def attach(node):
            if node["id"] in fetched and "children" not in node:
                node["children"] = fetched[node["id"]]
            for child in node.get("children", []):
                attach(child)

        for entry in tree_data.values():
            for root in entry.get("roots", []):
                attach(root)
        with open(os.path.join(data_dir, "ggsell_category_tree.json"), "w", encoding="utf-8") as f:
            json.dump(tree_data, f, ensure_ascii=False, indent=1)

    total = len(result) + sum(len(v) for v in unresolved.values())
    fees = Counter(v["fee"] for v in result.values())
    lines = [
        "# Подбор категорий GGSell — отчёт",
        "",
        f"Позиций: {total}. Подобрано: {len(result)}. Не подобрано: {total - len(result)}. "
        f"Через «Другое количество»/«Другие страны»: {sum(len(v) for v in fallback.values())}. "
        f"На проверку: {sum(len(v) for v in review.values())}.",
        "",
        "Комиссия GGSell (fee) по подобранным: "
        + ", ".join(f"{fee:.1%} — {n}" for fee, n in sorted(fees.items())),
        "",
        "## Не подобрано (нет подходящего листа на GGSell — нужно решение)",
    ]
    for reason, names in sorted(unresolved.items()):
        lines.append(f"- **{reason}** ({len(names)}): {', '.join(names)}")
    lines += ["", "## Подобрано через «Другое количество» / «Другие страны»"]
    for reason, names in sorted(fallback.items()):
        lines.append(f"- {reason} ({len(names)}): {', '.join(names)}")
    lines += ["", "## Помечено на проверку"]
    for reason, names in sorted(review.items()):
        lines.append(f"- {reason} ({len(names)}): {', '.join(names)}")
    with open(os.path.join(data_dir, "ggsell_category_report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print("\n".join(lines[:6]))
    print(f"Дозагружено узлов дерева: {len(fetched)}; пропущено тестовых позиций: {len(skipped)}")


if __name__ == "__main__":
    main()
