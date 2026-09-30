from unittest.mock import MagicMock

import pytest

from app.offers.categories import (
    CategoryNotResolved,
    CategoryTree,
    nominal_key,
    resolve_category,
    rules_for,
)


def leaf(id_, title, fee=0.02):
    return {"id": id_, "title": title, "tree": f"… > {title}", "fee": fee, "has_children": False}


def branch(id_, title, children, fee=0.15):
    return {"id": id_, "title": title, "tree": f"… > {title}", "fee": fee, "has_children": True, "children": children}


# Упрощённые куски реального дерева GGSell (30.09).
GENSHIN = branch(34747, "Genshin Impact", [
    branch(100398879, "Пополнение по ID", [
        leaf(62707, "Другое количество", 0.07),
        leaf(128644, "60", 0.07),
        leaf(128647, "300+30", 0.07),
    ]),
    leaf(100404013, "Благословение полой луны 30 дней"),
])
GOOGLE_PLAY = branch(76234, "Подарочные карты", [
    leaf(113803, "Индия (INR)", 0.04),
    leaf(115010, "Европа (EUR)", 0.04),
    leaf(115011, "США (USD)", 0.04),
    leaf(121069, "Другие страны", 0.04),
])
PUBG_GCOINS = branch(100402101, "Монеты/G-Coins", [leaf(51936, "Другое количество"), leaf(100402103, "510")])
LOL_RP = branch(120502, "RP", [
    branch(120510, "Северная Америка (NA)", [leaf(1, "575"), leaf(2, "Другое количество")]),
    branch(120507, "Европа (EU)", [leaf(3, "575")]),
    leaf(120512, "Другие страны", 0.05),
])
XBOX_KEYS = branch(107289, "Ключи", [leaf(107292, "PC", 0.04), leaf(107293, "Ultimate", 0.04)])
RAZER = branch(47657, "Razer Gold", [leaf(47659, "Аккаунты", 0.14)])

TREE = CategoryTree({
    "x": {"roots": [GENSHIN, GOOGLE_PLAY, PUBG_GCOINS, LOL_RP, XBOX_KEYS, RAZER]},
})


@pytest.mark.parametrize("text, key", [
    ("300 + 30 Genesis Crystals", "300+30"),
    ("300+30", "300+30"),
    ("20 000 Coins", "20000"),
    ("AUR125", "125"),
    ("90+", "90"),
    ("(iOS) 5 Rainbow Cards", "5"),
    ("Blessing of the Welkin Moon", None),
])
def test_nominal_key(text, key):
    assert nominal_key(text) == key


def test_nominal_exact_match():
    choice = resolve_category(TREE, [{"node": 100398879, "by": ["nominal"]}], "300 + 30 Genesis Crystals", "Global")
    assert choice.category_id == 128647
    assert choice.fee == 0.07
    assert not choice.via_fallback


def test_nominal_unknown_goes_to_other_quantity():
    choice = resolve_category(TREE, [{"node": 100398879, "by": ["nominal"]}], "6480 + 1600 Genesis Crystals", None)
    assert choice.category_id == 62707
    assert choice.via_fallback


def test_nominal_matches_sum():
    """G-Coins: FZ «500 + 10», GGSell лист «510»."""
    choice = resolve_category(TREE, [{"node": 100402101, "by": ["nominal"]}], "500 + 10 G-Coin", None)
    assert choice.category_id == 100402103
    assert not choice.via_fallback


def test_region_exact_and_eurozone_and_fallback():
    rules = [{"node": 76234, "by": ["region"]}]
    assert resolve_category(TREE, rules, "25 USD", "US").category_id == 115011
    assert resolve_category(TREE, rules, "15 EUR", "DE").category_id == 115010  # Германия → Европа
    other = resolve_category(TREE, rules, "25 GBP", "UK")  # UK не еврозона → «Другие страны»
    assert other.category_id == 121069 and other.via_fallback


def test_region_map_then_nominal():
    rules = [{"node": 120502, "by": ["region", "nominal"], "region_map": {"US": "NA", "UK": "EU"}}]
    assert resolve_category(TREE, rules, "575 RP", "US").category_id == 1
    assert resolve_category(TREE, rules, "575 RP", "UK").category_id == 3
    assert resolve_category(TREE, rules, "575 RP", "BR").category_id == 120512


def test_title_step_picks_tier():
    choice = resolve_category(TREE, [{"node": 107289, "by": ["title"]}], "3 Months Xbox Game Pass Ultimate", "US")
    assert choice.category_id == 107293


def test_first_matching_rule_wins_and_carries_review():
    rules = [
        {"match": "Welkin", "node": 100404013, "review": True, "note": "проверить"},
        {"match": "Genesis Crystals", "node": 100398879, "by": ["nominal"]},
    ]
    choice = resolve_category(TREE, rules, "Blessing of the Welkin Moon", None)
    assert choice.category_id == 100404013
    assert choice.review and choice.note == "проверить"


def test_null_node_is_unresolved_with_note():
    with pytest.raises(CategoryNotResolved, match="нет листа"):
        resolve_category(TREE, [{"node": None, "note": "нет листа"}], "10 USD", "US")


def test_non_leaf_result_is_rejected():
    """GGSell не даёт создать лот в категории с подкатегориями."""
    with pytest.raises(CategoryNotResolved, match="нужен лист"):
        resolve_category(TREE, [{"node": 47657}], "10 USD", "US")


def test_no_rule_matches():
    with pytest.raises(CategoryNotResolved, match="ни одно правило"):
        resolve_category(TREE, [{"match": "Coins", "node": 1}], "Golden Spin", None)


def test_missing_children_are_fetched():
    node = {"id": 9, "title": "Ветка", "tree": "… > Ветка", "fee": 0.15, "has_children": True}
    fetch = MagicMock(return_value=[leaf(10, "100")])
    tree = CategoryTree({"x": {"roots": [node]}}, fetch_children=fetch)

    choice = resolve_category(tree, [{"node": 9, "by": ["nominal"]}], "100 Diamonds", None)

    assert choice.category_id == 10
    fetch.assert_called_once_with(9)


def test_rules_for_exact_then_longest_glob():
    rules = {"steam_wallet_us": ["exact"], "steam_wallet_*": ["glob"], "steam_*": ["short"]}
    assert rules_for(rules, "steam_wallet_us") == ["exact"]
    assert rules_for(rules, "steam_wallet_br") == ["glob"]
    assert rules_for(rules, "steam_cis") == ["short"]
    assert rules_for(rules, "netflix_us") is None
