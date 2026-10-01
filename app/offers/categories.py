"""Подбор категории GGSell (category_id) для каждой позиции каталога.

Лот GGSell можно создать только в листовой категории (422 «Категория не
должна иметь дочерние категории», проверено 30 сентября), а дерево GGSell
доходит до конкретного номинала/страны: «Genshin Impact > Кристаллы
сотворения > Пополнение по ID > 300+30», «Google Play > Подарочные карты >
США (USD)». От листа зависит и комиссия GGSell (fee: 2% у валюты, 6–15% у
карт, до 49% у аккаунтов) — её учитывает расчёт цены.

Как устроено:
  - data/ggsell_category_rules.json — правила по категории FZ: какой товар
    (регулярка по названию оффера FZ) в какую ветку GGSell идёт и как от
    ветки спускаться к листу: по стране (region) и/или номиналу (nominal);
  - resolve_category() — спуск от ветки к листу; не нашли точного
    совпадения — «Другое количество» / «Другие страны», если они есть;
  - дерево — data/ggsell_category_tree.json (scripts/fetch_ggsell_categories.py),
    недостающих детей дозапрашиваем через fetch_children.

Позиция, для которой правило не подходит или лист не найден, не
«впихивается» куда попало, а возвращается с ошибкой — её решают вручную.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

from app.regions import REGION_NAMES

# Узлы-«остатки», куда идёт то, чего нет среди точных вариантов.
FALLBACK_TITLES = ("другое количество", "другие страны", "прочее", "other countries")


@dataclass
class CategoryChoice:
    category_id: int
    tree: str
    fee: float
    payment_fee: Optional[float]
    rule: str
    via_fallback: bool = False  # лист выбран как «Другое количество»/«Другие страны»
    review: bool = False  # правило помечено как неочевидное — показать на проверку
    note: Optional[str] = None


class CategoryNotResolved(Exception):
    pass


def nominal_key(text: str) -> Optional[str]:
    """Номинал из названия FZ или категории GGSell в сравнимом виде:
    «300 + 30 Genesis Crystals» → "300+30", «20 000 Coins» → "20000",
    «AUR125» → "125", «1000 coins» → "1000", «90+» → "90"."""
    cleaned = re.sub(r"(?<=\d)[  ](?=\d{3}\b)", "", text)  # «20 000» → «20000»
    numbers = re.findall(r"\d+", cleaned)
    if not numbers:
        return None
    if re.search(r"\d\s*\+\s*\d", cleaned):
        match = re.search(r"(\d+)\s*\+\s*(\d+)", cleaned)
        return f"{match.group(1)}+{match.group(2)}"
    return numbers[0]


def _norm(text: str) -> str:
    return " ".join(text.split()).casefold()


def _is_fallback(node: dict[str, Any]) -> bool:
    return any(t in _norm(node["title"]) for t in FALLBACK_TITLES)


def pick_by_nominal(children: list[dict[str, Any]], offer_name: str) -> tuple[dict[str, Any], bool]:
    key = nominal_key(offer_name)
    if key is not None:
        for child in children:
            if not _is_fallback(child) and nominal_key(child["title"]) == key:
                return child, False
        # «500 + 10 G-Coin» у FZ против «510» у GGSell — по сумме.
        if "+" in key:
            total = str(sum(int(x) for x in key.split("+")))
            for child in children:
                if not _is_fallback(child) and nominal_key(child["title"]) == total:
                    return child, False
        # «300+30» у FZ против «300» у GGSell (или наоборот) — по основной части.
        base = key.split("+")[0]
        for child in children:
            ck = nominal_key(child["title"])
            if not _is_fallback(child) and ck is not None and ck.split("+")[0] == base:
                return child, False
    for child in children:
        if _is_fallback(child):
            return child, True
    raise CategoryNotResolved(f"номинал {key!r} из {offer_name!r} не найден среди {[c['title'] for c in children]}")


def pick_by_region(children: list[dict[str, Any]], region: str) -> tuple[dict[str, Any], bool]:
    names = REGION_NAMES.get(region.upper(), [region])
    for name in names:
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
        for child in children:
            if not _is_fallback(child) and pattern.search(child["title"]):
                return child, False
    for child in children:
        if _is_fallback(child):
            return child, True
    raise CategoryNotResolved(f"регион {region!r} не найден среди {[c['title'] for c in children]}")


def pick_by_title(children: list[dict[str, Any]], offer_name: str) -> tuple[dict[str, Any], bool]:
    """Лист, название которого целым словом есть в названии оффера FZ
    (тарифы: «... Xbox Game Pass Ultimate» → лист «Ultimate»). Самое длинное
    совпадение выигрывает."""
    matches = [
        child
        for child in children
        if not _is_fallback(child)
        and re.search(rf"(?<!\w){re.escape(child['title'].strip())}(?!\w)", offer_name, re.IGNORECASE)
    ]
    if matches:
        return max(matches, key=lambda c: len(c["title"])), False
    for child in children:
        if _is_fallback(child):
            return child, True
    raise CategoryNotResolved(f"в {offer_name!r} нет ни одного из {[c['title'] for c in children]}")


class CategoryTree:
    """Индекс узлов дерева GGSell по id + дозагрузка недостающих детей."""

    def __init__(
        self,
        tree_data: dict[str, Any],
        fetch_children: Optional[Callable[[int], list[dict[str, Any]]]] = None,
    ):
        self.nodes: dict[int, dict[str, Any]] = {}
        self._fetch = fetch_children
        for entry in tree_data.values():
            for root in entry.get("roots", []):
                self._index(root)

    def _index(self, node: dict[str, Any]) -> None:
        existing = self.nodes.get(node["id"])
        if existing is None or ("children" in node and "children" not in existing):
            self.nodes[node["id"]] = node
        for child in node.get("children", []):
            self._index(child)

    def node(self, node_id: int) -> dict[str, Any]:
        if node_id not in self.nodes:
            raise CategoryNotResolved(f"узел {node_id} отсутствует в дереве GGSell")
        return self.nodes[node_id]

    def children(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        if not node.get("has_children"):
            return []
        if "children" not in node:
            if self._fetch is None:
                raise CategoryNotResolved(f"дети узла {node['id']} ({node['tree']}) не загружены")
            node["children"] = self._fetch(node["id"])
            for child in node["children"]:
                self._index(child)
        return node["children"]


def rules_for(all_rules: dict[str, Any], fz_category_id: str) -> Optional[list[dict[str, Any]]]:
    """Правила категории FZ: точный ключ, иначе самый длинный подходящий
    шаблон с * (google_play_* и т.п.)."""
    if fz_category_id in all_rules:
        return all_rules[fz_category_id]
    matches = [k for k in all_rules if "*" in k and fnmatch.fnmatchcase(fz_category_id, k)]
    return all_rules[max(matches, key=len)] if matches else None


def resolve_category(
    tree: CategoryTree,
    rules: list[dict[str, Any]],
    offer_name: str,
    region: Optional[str],
) -> CategoryChoice:
    """rules — список правил категории FZ, первое подходящее выигрывает:
        {"match": "<regex по названию оффера FZ>"   (нет — подходит всё),
         "node": <id ветки GGSell>,
         "by": ["region", "nominal", "title"]        (порядок спуска, можно пусто),
         "region_map": {"US": "NA"}                  (регион позиции → регион в дереве,
                                                      когда деление не по странам: сервера LoL),
         "note": "..."}
    Из ветки спускаемся по шагам `by`; в конце должен получиться лист."""
    for rule in rules:
        if rule.get("match") and not re.search(rule["match"], offer_name, re.IGNORECASE):
            continue
        if rule.get("node") is None:
            raise CategoryNotResolved(rule.get("note") or f"для {offer_name!r} нет подходящей категории GGSell")
        node = tree.node(rule["node"])
        via_fallback = False
        for step in rule.get("by", []):
            children = tree.children(node)
            if not children:
                break
            if step == "region":
                if not region:
                    raise CategoryNotResolved(f"правило требует регион, а у позиции {offer_name!r} его нет")
                mapped = rule.get("region_map", {}).get(region.upper(), region)
                node, fb = pick_by_region(children, mapped)
            elif step == "nominal":
                node, fb = pick_by_nominal(children, offer_name)
            elif step == "title":
                node, fb = pick_by_title(children, offer_name)
            else:
                raise ValueError(f"неизвестный шаг {step!r}")
            via_fallback = via_fallback or fb
        if node.get("has_children"):
            raise CategoryNotResolved(
                f"{offer_name!r}: правило привело к {node['tree']!r}, а у неё есть подкатегории — нужен лист"
            )
        return CategoryChoice(
            category_id=node["id"],
            tree=node["tree"],
            fee=node["fee"],
            payment_fee=node.get("payment_fee"),
            rule=rule.get("match") or "*",
            via_fallback=via_fallback,
            review=bool(rule.get("review")),
            note=rule.get("note"),
        )
    raise CategoryNotResolved(f"ни одно правило не подошло к {offer_name!r}")
