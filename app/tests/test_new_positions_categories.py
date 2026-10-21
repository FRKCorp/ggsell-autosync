"""Новые позиции FZ, которых нет в готовой карте категорий: залив подбирает
категорию по тем же правилам и дереву GGSell (data/), что и build_category_map."""

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.models.position import Position, SourceType
from app.offers.categories import CategoryTree, resolve_positions

DATA = Path(__file__).resolve().parents[2] / "data"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "upload_offers.py"


def load(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def rules():
    return load("ggsell_category_rules.json")


@pytest.fixture
def tree():
    return CategoryTree(load("ggsell_category_tree.json"))  # без запросов к GGSell


def position(category, offer, name, region, source=SourceType.GIFTCARD):
    kind = "giftcard" if source == SourceType.GIFTCARD else "topup"
    return Position(external_id=f"{kind}:{category}:{offer}", source_type=source, fz_category_id=category,
                    fz_offer_id=offer, name=name, region=region, last_known_price_usd=Decimal("10"),
                    raw_payload={"name": name})


def test_app_store_us_and_hk(rules, tree):
    us = position("app_store_itunes_us", "10_usd", "10 USD", "US")
    hk = position("app_store_itunes_hk", "100_hkd", "100 HKD", "HK")

    resolved, failed = resolve_positions([us, hk], rules, tree)

    assert failed == {}
    assert resolved[us.external_id]["tree"].endswith("Apple ID > Подарочные карты > США (USD)")
    assert not resolved[us.external_id]["via_fallback"]
    # Отдельного листа Гонконга на GGSell нет — «Другие страны», с пометкой на проверку.
    assert resolved[hk.external_id]["tree"].endswith("Другие страны")
    assert resolved[hk.external_id]["via_fallback"]


def test_new_wild_rift_regions(rules, tree):
    by = position("wild_rift_by", "425_wc", "425 Wild Cores", "BY")

    resolved, failed = resolve_positions([by], rules, tree)

    assert failed == {} and "Wild Rift" in resolved[by.external_id]["tree"]


@pytest.mark.parametrize("name, leaf", [
    ("500 Coins", "Монеты"),
    ("Premium (30 days)", "Премиум"),
    ("Battle Pass", "Боевой пропуск"),
    ("Mystery Bundle", "Other"),
])
def test_oxide_items_by_type(rules, tree, name, leaf):
    p = position("oxide_survival_island", "x", name, "Global", source=SourceType.TOPUP)

    resolved, failed = resolve_positions([p], rules, tree)

    assert failed == {}
    assert resolved[p.external_id]["tree"].endswith(leaf)
    assert resolved[p.external_id]["review"] == (leaf == "Other")


def test_unmappable_positions_are_reported_not_uploaded(rules, tree):
    razer = position("razer_gold_global", "10_usd", "10 USD", "Global")
    unknown = position("some_new_service", "x", "Card", "Global")

    resolved, failed = resolve_positions([razer, unknown], rules, tree)

    assert resolved == {}
    assert "Razer Gold" in failed[razer.external_id]
    assert "нет правил" in failed[unknown.external_id]


def test_only_accepts_prefix_and_wildcard():
    spec = importlib.util.spec_from_file_location("upload_offers", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.matches_only("giftcard:app_store_itunes_us:10_usd", [])
    assert module.matches_only("giftcard:app_store_itunes_us:10_usd", ["giftcard:app_store_itunes_us"])
    assert module.matches_only("giftcard:app_store_itunes_us:10_usd", ["giftcard:app_store_itunes_us*"])
    assert not module.matches_only("giftcard:app_store_itunes_uk:10_gbp", ["giftcard:app_store_itunes_us*"])
