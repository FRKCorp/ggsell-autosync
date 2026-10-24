"""Групповые лоты: номиналы одной категории FZ в одном оффере GGSell.

Какие категории групповые — data/grouped_categories.json. В группу входят
номиналы, которые есть у FZ и не дороже лимита; базовый номинал — самый
дешёвый (его цена — цена лота, у остальных вариантов — надбавка).
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

from app.models.position import Position
from app.offers.categories import nominal_key

CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "grouped_categories.json"


@dataclass(frozen=True)
class GroupRule:
    max_price_usd: Decimal
    option_title_ru: str = "Номинал"
    option_title_en: str = "Denomination"


@dataclass
class GroupSelection:
    """Номиналы одной категории FZ: members — в продажу (по возрастанию цены,
    первый — базовый), остальные — с причиной, почему не в группе."""

    fz_category_id: str
    rule: GroupRule
    members: list[Position] = field(default_factory=list)
    over_limit: list[Position] = field(default_factory=list)
    unavailable: list[Position] = field(default_factory=list)

    @property
    def base(self) -> Optional[Position]:
        return self.members[0] if self.members else None


@lru_cache(maxsize=None)
def load_group_config(path: Path = CONFIG_PATH) -> dict[str, GroupRule]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return {
        key: GroupRule(
            max_price_usd=Decimal(str(value["max_price_usd"])),
            option_title_ru=value.get("option_title_ru", "Номинал"),
            option_title_en=value.get("option_title_en", "Denomination"),
        )
        for key, value in raw.items()
        if not key.startswith("_")
    }


def group_rule_for(fz_category_id: Optional[str], config: Optional[dict[str, GroupRule]] = None) -> Optional[GroupRule]:
    """Правило группы для категории FZ: точный ключ, иначе самый длинный шаблон с *."""
    if not fz_category_id:
        return None
    config = load_group_config() if config is None else config
    if fz_category_id in config:
        return config[fz_category_id]
    matches = [k for k in config if "*" in k and fnmatch.fnmatchcase(fz_category_id, k)]
    return config[max(matches, key=len)] if matches else None


def _sort_key(position: Position) -> tuple[Decimal, float]:
    """По цене FZ; при равной цене — по номиналу из названия."""
    nominal = nominal_key((position.raw_payload or {}).get("name", position.name) or "")
    try:
        number = float(nominal.split("+")[0]) if nominal else 0.0
    except ValueError:
        number = 0.0
    return position.last_known_price_usd or Decimal(0), number


def select_group(fz_category_id: str, positions: Iterable[Position], rule: GroupRule) -> GroupSelection:
    selection = GroupSelection(fz_category_id=fz_category_id, rule=rule)
    for position in sorted(positions, key=_sort_key):
        price = position.last_known_price_usd
        if position.fz_missing_since is not None or not price or price <= 0:
            selection.unavailable.append(position)
        elif price > rule.max_price_usd:
            selection.over_limit.append(position)
        else:
            selection.members.append(position)
    return selection


def group_positions(
    positions: Iterable[Position], config: Optional[dict[str, GroupRule]] = None
) -> dict[str, GroupSelection]:
    """{category_id FZ: GroupSelection} для позиций групповых категорий;
    остальные позиции пропускаются."""
    config = load_group_config() if config is None else config
    by_category: dict[str, list[Position]] = {}
    rules: dict[str, GroupRule] = {}
    for position in positions:
        rule = group_rule_for(position.fz_category_id, config)
        if rule is None:
            continue
        by_category.setdefault(position.fz_category_id, []).append(position)
        rules[position.fz_category_id] = rule
    return {category: select_group(category, items, rules[category]) for category, items in sorted(by_category.items())}


def is_grouped(position: Any, config: Optional[dict[str, GroupRule]] = None) -> bool:
    return group_rule_for(getattr(position, "fz_category_id", None), config) is not None
