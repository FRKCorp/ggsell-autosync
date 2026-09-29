from unittest.mock import MagicMock

import pytest

from app.offers.options import (
    PLACEHOLDER_RU,
    BuyerDataError,
    attach_topup_options,
    build_topup_options,
    map_buyer_data_to_fz_fields,
)

# Реальные fields из data/fz_topup_fields.json (выгружены 30 сентября).
MLBB_FIELDS = [
    {"key": "player_id", "label": "Player ID", "type": "text"},
    {"key": "server_id", "label": "Server ID", "type": "text"},
]
WUWA_FIELDS = [
    {"key": "player_id", "label": "Player ID", "type": "text"},
    {
        "key": "server",
        "label": "Server",
        "type": "select",
        "options": [
            {"label": "Asia", "value": "os_asia"},
            {"label": "America", "value": "os_usa"},
            {"label": "TW/HK/MO", "value": "os_cht"},
        ],
    },
]
BALL_POOL_FIELDS = [{"key": "user_id", "label": "Unique ID", "type": "text"}]


# ----------------------------------------------------------------------
# build_topup_options
# ----------------------------------------------------------------------


def test_text_fields_become_required_text_options():
    specs = build_topup_options(MLBB_FIELDS)

    assert [s.fz_key for s in specs] == ["player_id", "server_id"]
    first = specs[0].option
    assert first["type"] == "text"
    assert first["is_required"] is True
    assert first["status"] == "active"
    assert first["title_ru"] == "ID игрока (Player ID)"
    assert first["title_en"] == "Player ID"
    assert [s.option["position"] for s in specs] == [0, 1]
    assert all(s.variants == [] for s in specs)


def test_unknown_label_is_kept_as_is():
    [spec] = build_topup_options(BALL_POOL_FIELDS)
    assert spec.option["title_ru"] == spec.option["title_en"] == "Unique ID"


def test_select_becomes_radio_button_with_free_variants():
    server = build_topup_options(WUWA_FIELDS)[1]

    assert server.option["type"] == "radio_button"
    assert server.option["title_ru"] == "Сервер"
    assert "variants" not in server.option  # варианты — отдельным запросом
    assert [v["title_ru"] for v in server.variants] == [PLACEHOLDER_RU, "Asia", "America", "TW/HK/MO"]
    assert all(v["price"] == 0 and v["impact_type"] == "increase" for v in server.variants)
    assert [v["position"] for v in server.variants] == [0, 1, 2, 3]


def test_select_default_is_placeholder_not_a_real_server():
    """GGSell требует ровно один вариант по умолчанию; реальный сервер им
    быть не должен, иначе невнимательный покупатель получит пополнение не туда."""
    server = build_topup_options(WUWA_FIELDS)[1]
    defaults = [v for v in server.variants if v["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["title_ru"] == PLACEHOLDER_RU


def test_no_fields_no_options():
    assert build_topup_options([]) == []


# ----------------------------------------------------------------------
# map_buyer_data_to_fz_fields
# ----------------------------------------------------------------------


def test_maps_russian_titles_to_fz_keys():
    buyer_data = {"ID игрока (Player ID)": " 12345 ", "ID сервера (Server ID)": "6789"}
    assert map_buyer_data_to_fz_fields(MLBB_FIELDS, buyer_data) == {
        "player_id": "12345",
        "server_id": "6789",
    }


def test_maps_english_titles_and_raw_keys():
    assert map_buyer_data_to_fz_fields(MLBB_FIELDS, {"Player ID": "1", "server_id": "2"}) == {
        "player_id": "1",
        "server_id": "2",
    }


def test_title_match_ignores_case_and_spaces():
    assert map_buyer_data_to_fz_fields(BALL_POOL_FIELDS, {"  unique   id ": "abc"}) == {
        "user_id": "abc"
    }


def test_select_label_translated_to_fz_value():
    result = map_buyer_data_to_fz_fields(WUWA_FIELDS, {"Player ID": "1", "Сервер": "Asia"})
    assert result == {"player_id": "1", "server": "os_asia"}


def test_select_accepts_fz_value_directly():
    result = map_buyer_data_to_fz_fields(WUWA_FIELDS, {"Player ID": "1", "Server": "os_cht"})
    assert result["server"] == "os_cht"


def test_missing_field_raises():
    with pytest.raises(BuyerDataError, match="Server ID"):
        map_buyer_data_to_fz_fields(MLBB_FIELDS, {"Player ID": "1"})


def test_empty_value_counts_as_missing():
    with pytest.raises(BuyerDataError):
        map_buyer_data_to_fz_fields(BALL_POOL_FIELDS, {"Unique ID": ""})


def test_unknown_select_variant_raises():
    with pytest.raises(BuyerDataError, match="Europe"):
        map_buyer_data_to_fz_fields(WUWA_FIELDS, {"Player ID": "1", "Сервер": "Europe"})


def test_placeholder_left_selected_raises():
    with pytest.raises(BuyerDataError, match="заглушка"):
        map_buyer_data_to_fz_fields(WUWA_FIELDS, {"Player ID": "1", "Сервер": PLACEHOLDER_RU})


def test_extra_buyer_options_are_ignored():
    result = map_buyer_data_to_fz_fields(BALL_POOL_FIELDS, {"Unique ID": "1", "Комментарий": "x"})
    assert result == {"user_id": "1"}


# ----------------------------------------------------------------------
# attach_topup_options
# ----------------------------------------------------------------------


def _options(*items):
    return {"data": [dict(zip(("id", "title_ru", "variants"), item)) for item in items]}


def test_attach_creates_options_then_variants_by_option_id():
    v2 = MagicMock()
    v2.list_offer_options.side_effect = [
        _options(),  # до создания — пусто
        _options((11, "ID игрока (Player ID)", []), (12, "Сервер", [])),
        _options((11, "ID игрока (Player ID)", []), (12, "Сервер", [{"id": 1}])),
    ]

    attach_topup_options(v2, 555, WUWA_FIELDS)

    offer_id, options = v2.create_or_update_options.call_args.args
    assert offer_id == 555
    assert [o["type"] for o in options] == ["text", "radio_button"]
    offer_id, option_id, variants = v2.create_or_update_variants.call_args.args
    assert (offer_id, option_id) == (555, 12)
    assert len(variants) == 4  # заглушка + 3 сервера


def test_attach_text_only_skips_variants():
    v2 = MagicMock()
    v2.list_offer_options.side_effect = [[], []]

    attach_topup_options(v2, 555, MLBB_FIELDS)

    v2.create_or_update_options.assert_called_once()
    v2.create_or_update_variants.assert_not_called()


def test_attach_fails_loudly_if_created_option_missing():
    v2 = MagicMock()
    v2.list_offer_options.side_effect = [_options(), _options()]

    with pytest.raises(RuntimeError, match="Сервер"):
        attach_topup_options(v2, 555, WUWA_FIELDS)


def test_attach_rerun_after_full_success_does_nothing():
    v2 = MagicMock()
    done = _options((11, "ID игрока (Player ID)", []), (12, "Сервер", [{"id": 1}]))
    v2.list_offer_options.return_value = done

    attach_topup_options(v2, 555, WUWA_FIELDS)

    v2.create_or_update_options.assert_not_called()
    v2.create_or_update_variants.assert_not_called()


def test_attach_rerun_after_variants_failed_creates_only_variants():
    """Реальный сценарий 30.09: опции создались, варианты упали с 422 —
    повторный запуск не плодит дубли опций, а досоздаёт варианты."""
    v2 = MagicMock()
    v2.list_offer_options.return_value = _options(
        (11, "ID игрока (Player ID)", []), (12, "Сервер", [])
    )

    attach_topup_options(v2, 555, WUWA_FIELDS)

    v2.create_or_update_options.assert_not_called()
    offer_id, option_id, _ = v2.create_or_update_variants.call_args.args
    assert option_id == 12


def test_all_real_fz_fields_roundtrip():
    """Для каждой из 40 реальных категорий: опции строятся, а данные,
    введённые под их названиями, переводятся обратно в ключи/значения FZ."""
    import json
    from pathlib import Path

    data = json.loads(
        (Path(__file__).resolve().parents[2] / "data" / "fz_topup_fields.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(data) >= 40
    for category_id, category in data.items():
        fields = category["fields"]
        specs = build_topup_options(fields)
        buyer_data = {}
        expected = {}
        for fz_field, spec in zip(fields, specs):
            if fz_field["type"] == "select":
                choice = fz_field["options"][-1]
                buyer_data[spec.option["title_ru"]] = choice["label"]
                expected[fz_field["key"]] = choice["value"]
            else:
                buyer_data[spec.option["title_ru"]] = "id-1"
                expected[fz_field["key"]] = "id-1"
        assert map_buyer_data_to_fz_fields(fields, buyer_data) == expected, category_id
