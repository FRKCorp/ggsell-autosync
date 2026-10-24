"""Выбор номиналов групповых лотов на реальных данных App Store (цены FZ,
пропавшие номиналы)."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.models.position import Position, SourceType
from app.offers.groups import GroupRule, group_positions, group_rule_for, is_grouped, load_group_config

# category; offer; name; price_usd; missing at FZ
DATA = """
app_store_itunes_ae;50_aed;50 AED;13.40270000;f
app_store_itunes_ae;75_aed;75 AED;20.09900000;f
app_store_itunes_ae;100_aed;100 AED;26.80540000;f
app_store_itunes_ae;150_aed;150 AED;40.20810000;f
app_store_itunes_ae;200_aed;200 AED;53.61080000;f
app_store_itunes_ae;250_aed;250 AED;67.01350000;f
app_store_itunes_ae;300_aed;300 AED;80.41620000;f
app_store_itunes_ae;400_aed;400 AED;107.22160000;f
app_store_itunes_ae;500_aed;500 AED;134.02700000;f
app_store_itunes_ae;750_aed;750 AED;208.13790000;t
app_store_itunes_ae;5000_aed;5000 AED;1340.24980000;f
app_store_itunes_ca;200_cad;200 CAD;145.44590000;t
app_store_itunes_ru;500_rub;500 RUB;7.37640000;f
app_store_itunes_ru;600_rub;600 RUB;8.23700000;f
app_store_itunes_ru;700_rub;700 RUB;10.32700000;f
app_store_itunes_ru;800_rub;800 RUB;11.87000000;f
app_store_itunes_ru;900_rub;900 RUB;13.35000000;f
app_store_itunes_ru;1000_rub;1000 RUB;14.75280000;f
app_store_itunes_ru;1500_rub;1500 RUB;22.12920000;f
app_store_itunes_ru;2000_rub;2000 RUB;29.66000000;f
app_store_itunes_ru;3000_rub;3000 RUB;44.50000000;f
app_store_itunes_ru;4000_rub;4000 RUB;59.33000000;f
app_store_itunes_ru;5000_rub;5000 RUB;72.29960000;f
app_store_itunes_ru;6000_rub;6000 RUB;88.99000000;f
app_store_itunes_ru;7000_rub;7000 RUB;103.82000000;f
app_store_itunes_ru;8000_rub;8000 RUB;118.66000000;f
app_store_itunes_tr;10_try;10 TRY;0.20640000;f
app_store_itunes_tr;25_try;25 TRY;0.51580000;f
app_store_itunes_tr;799_try;799 TRY;17.39510000;t
app_store_itunes_tr;2000_try;2000 TRY;41.03630000;f
app_store_itunes_uk;2_gbp;2 GBP;2.73380000;t
app_store_itunes_uk;5_gbp;5 GBP;6.58520000;f
app_store_itunes_uk;10_gbp;10 GBP;13.17040000;f
app_store_itunes_uk;50_gbp;50 GBP;67.27610000;f
app_store_itunes_uk;75_gbp;75 GBP;102.51580000;t
app_store_itunes_uk;100_gbp;100 GBP;134.54210000;f
google_play_us;10_usd;10 USD;10.2;f
"""


def positions():
    result = []
    for line in DATA.strip().splitlines():
        category, offer, name, price, missing = line.split(";")
        result.append(Position(
            external_id=f"giftcard:{category}:{offer}", source_type=SourceType.GIFTCARD, fz_category_id=category,
            fz_offer_id=offer, name=f"{category} — {name}", raw_payload={"name": name},
            last_known_price_usd=Decimal(price),
            fz_missing_since=datetime(2026, 10, 1, tzinfo=timezone.utc) if missing == "t" else None,
        ))
    return result


def offers(items):
    return [p.fz_offer_id for p in items]


def test_config_has_app_store_with_100_usd_limit_and_tolerance():
    rule = group_rule_for("app_store_itunes_us")
    assert rule.max_price_usd == Decimal("105") and rule.option_title_ru == "Номинал"
    assert group_rule_for("google_play_us") is None and group_rule_for(None) is None


def test_russia_matches_competitor_range_500_to_7000():
    groups = group_positions(positions())

    ru = groups["app_store_itunes_ru"]
    assert offers(ru.members) == ["500_rub", "600_rub", "700_rub", "800_rub", "900_rub", "1000_rub", "1500_rub",
                                  "2000_rub", "3000_rub", "4000_rub", "5000_rub", "6000_rub", "7000_rub"]
    assert ru.base.fz_offer_id == "500_rub"
    assert offers(ru.over_limit) == ["8000_rub"] and ru.unavailable == []


def test_missing_and_expensive_nominals_left_out():
    groups = group_positions(positions())

    uk = groups["app_store_itunes_uk"]
    assert offers(uk.members) == ["5_gbp", "10_gbp", "50_gbp"]  # 2 GBP пропал у FZ — база 5 GBP
    assert offers(uk.unavailable) == ["2_gbp", "75_gbp"] and offers(uk.over_limit) == ["100_gbp"]

    ae = groups["app_store_itunes_ae"]
    assert ae.members[-1].fz_offer_id == "300_aed"  # 400 AED = $107 — дороже лимита
    assert offers(ae.over_limit) == ["400_aed", "500_aed", "5000_aed"]

    tr = groups["app_store_itunes_tr"]
    assert offers(tr.members) == ["10_try", "25_try", "2000_try"] and offers(tr.unavailable) == ["799_try"]


def test_region_without_sellable_nominals_has_no_base():
    ca = group_positions(positions())["app_store_itunes_ca"]

    assert ca.members == [] and ca.base is None


def test_non_group_categories_skipped():
    groups = group_positions(positions())

    assert "google_play_us" not in groups
    assert is_grouped(positions()[0]) and not is_grouped(positions()[-1])


def test_equal_prices_sorted_by_nominal():
    a, b = positions()[:2]
    a.last_known_price_usd = b.last_known_price_usd = Decimal("5")
    rule = GroupRule(max_price_usd=Decimal("100"))

    group = group_positions([b, a], {"app_store_itunes_*": rule})["app_store_itunes_ae"]

    assert offers(group.members) == ["50_aed", "75_aed"]


@pytest.fixture(autouse=True)
def fresh_config():
    load_group_config.cache_clear()
