"""Сборка карточки (payload create_offer) для позиции каталога (roadmap 3.5).

По образцу клиента от 01.10 (notes 6.11):
    «Mobile Legends RU* Алмазы 275 (250+25) | по ID | Автодоставка»
Тексты — в data/offer_templates.json, перевод названий товаров — в
data/offer_terms.json (их можно править без кода). Здесь — только логика:
разбор названия номинала FZ, регион, поля покупателя, цена, категория.

Что откуда:
  - категория GGSell и её комиссии — data/ggsell_category_map.json
    (app/offers/categories.py); нет категории — позицию не выставляем (2.13);
  - цена — app/pricing/listing_price.py (курс ЦБ + надбавка, наценка,
    комиссии категории, вверх до рубля);
  - регион — Position.region (app/regions.py, roadmap 1.9);
  - поля покупателя для описания — fields FZ (data/fz_topup_fields.json);
    сами опции на оффер вешает attach_topup_options (app/offers/options.py);
  - обложка — заглушка data/placeholder_cover.png: обложки клиент ставит сам
    (решение 2.3), лоты создаются только черновиками (2.6).
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from app.models.position import Position, SourceType
from app.pricing.calculator import GGSellFees, PricingConfig
from app.pricing.listing_price import price_rub
from app.regions import GLOBAL, REGION_NAMES

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@lru_cache(maxsize=None)
def _load_json(name: str) -> dict[str, Any]:
    return json.loads((DATA_DIR / name).read_text(encoding="utf-8"))


def load_templates() -> dict[str, Any]:
    return _load_json("offer_templates.json")


def load_terms() -> dict[str, Any]:
    return _load_json("offer_terms.json")


@lru_cache(maxsize=None)
def placeholder_cover_base64() -> str:
    data = (DATA_DIR / "placeholder_cover.png").read_bytes()
    return "data:image/png;base64," + base64.b64encode(data).decode("ascii")


# ----------------------------------------------------------------------
# Название товара
# ----------------------------------------------------------------------

_CURRENCY_ONLY = re.compile(r"^\s*(\d[\d\s.,]*)\s+([A-Z]{3})\s*$")  # «25 USD» у подарочных карт
_MONTHS = re.compile(r"\b(\d+)\s*Months?\b", re.IGNORECASE)
_LEADING_QTY = re.compile(r"^\s*(\d[\d ]*)(?:\s*\+\s*(\d[\d ]*))?\s+(.+?)\s*$")  # «250 + 25 Diamonds»
_GLUED_QTY = re.compile(r"^\s*([A-Za-z]+)\s*(\d+)\s*$")  # «AUR125»
_TRAILING_QTY = re.compile(r"^\s*(\D+?)\s+(\d+)\s*$")  # «Level Up Package 6»
# «300 Crystals + 30 Diamonds» — две разные валюты
_PAIR_QTY = re.compile(r"^\s*(\d[\d ]*)\s+(\D+?)\s*\+\s*(\d[\d ]*)\s+(\D+?)\s*$")
_PARENS = re.compile(r"\s*\(([^()]*)\)")


def _int(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def _translate(term: str, terms: dict[str, str]) -> str:
    lookup = {k.casefold(): v for k, v in terms.items()}
    return lookup.get(" ".join(term.split()).casefold(), term)


def format_item(
    offer_name: str,
    *,
    source_type: SourceType,
    region: Optional[str],
    lang: str,
    terms_data: Optional[dict[str, Any]] = None,
) -> str:
    """Название товара для карточки из названия номинала FZ:
        «250 + 25 Diamonds»     → «Алмазы 275 (250+25)»
        «60 UC»                 → «UC 60»
        «AUR125»                → «AUR 125»
        «Weekly Pass»           → «Недельный пропуск»
        «1 Month (UK) Standard» → «Standard 1 мес.» (регион — в названии карточки)
        «(iOS) 5 Rainbow Cards» → «Радужные карты 5 (iOS)»
        giftcard «25 USD»       → «Подарочная карта 25 USD»
    lang="en" — то же без перевода терминов."""
    terms_data = terms_data or load_terms()
    terms = terms_data["terms"] if lang == "ru" else {}
    name = " ".join(offer_name.split())

    if source_type == SourceType.GIFTCARD:
        match = _CURRENCY_ONLY.match(name)
        if match:
            return f"{terms_data['giftcard_item'][lang]} {match.group(1).strip()} {match.group(2)}"

    # Пометки в скобках: регион позиции убираем (он есть в названии карточки),
    # остальное (платформа и т.п.) переносим в конец.
    extras = []
    for inner in _PARENS.findall(name):
        if not (region and inner.strip().casefold() == region.casefold()):
            extras.append(inner.strip())
    name = _PARENS.sub("", name).strip()

    months = None
    match = _MONTHS.search(name)
    if match:
        months = int(match.group(1))
        name = " ".join((name[: match.start()] + name[match.end():]).split()).strip(" :-")

    pair = _PAIR_QTY.match(name)
    match = _LEADING_QTY.match(name)
    glued = _GLUED_QTY.match(name)
    trailing = _TRAILING_QTY.match(name)
    if pair:
        item = (
            f"{_translate(pair.group(2), terms)} {_int(pair.group(1))} + "
            f"{_translate(pair.group(4), terms)} {_int(pair.group(3))}"
        )
    elif match:
        base = _int(match.group(1))
        bonus = _int(match.group(2)) if match.group(2) else None
        term = _translate(match.group(3), terms)
        item = f"{term} {base + bonus} ({base}+{bonus})" if bonus else f"{term} {base}"
    elif glued:
        item = f"{_translate(glued.group(1), terms)} {glued.group(2)}"
    elif trailing and _translate(trailing.group(1), terms) != trailing.group(1):
        item = f"{_translate(trailing.group(1), terms)} {trailing.group(2)}"
    else:
        item = _translate(name, terms)

    if months:
        item = f"{item} {months} {terms_data['months'][lang]}".strip()
    if extras:
        item = f"{item} ({', '.join(extras)})"
    return item


def game_name(position: Position) -> str:
    """Игра/сервис без пометок региона и варианта: «Mobile Legends (RU) — 35
    Diamonds» → «Mobile Legends». Регион в карточке пишется отдельно, вариант
    (Auto, Tier 1…) покупателю не нужен. Остальное в скобках — платформа и
    т.п. — сохраняем: «Apex Legends™ (EA, Global)» → «Apex Legends™ (EA)»."""
    category_part = position.name.split(" — ", 1)[0]
    drop = {x.casefold() for x in (position.region, position.variant_label, GLOBAL) if x}

    def keep_only_meaningful(match: re.Match) -> str:
        parts = [part.strip() for part in match.group(1).split(",")]
        kept = [part for part in parts if part.casefold() not in drop]
        return f" ({', '.join(kept)})" if kept else ""

    return " ".join(_PARENS.sub(keep_only_meaningful, category_part).split())


# ----------------------------------------------------------------------
# Регион и поля
# ----------------------------------------------------------------------


def region_name(region: str, lang: str) -> str:
    names = REGION_NAMES.get(region.upper(), [region])
    if lang == "ru":
        return next((n for n in names if re.search("[а-яА-Я]", n)), names[0])
    return next((n for n in names if not re.search("[а-яА-Я]", n)), names[0])


def region_text(region: Optional[str], source_type: SourceType, lang: str, templates: dict[str, Any]) -> str:
    texts = templates["regions"][source_type.value][lang]
    region = region or GLOBAL
    if region in texts:
        return texts[region]
    return texts["_default"].format(region_name=region_name(region, lang))


def _join(parts: list[str], lang: str, templates: dict[str, Any]) -> str:
    joiner = templates["fields"][f"and_{lang}"]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + joiner + parts[-1]


def fields_text(
    fz_fields: list[dict[str, Any]], lang: str, templates: dict[str, Any]
) -> tuple[str, str, int, bool]:
    """Поля покупателя для текста: (по чему пополнение — «ID игрока и серверу»;
    что вводить — «ID игрока»; сколько полей вводить; есть ли выбор из списка)."""
    by = templates["fields"][f"by_{lang}"]
    enter = templates["fields"][f"enter_{lang}"]
    fields = fz_fields or [{"key": "player_id", "label": "Player ID", "type": "text"}]
    by_parts = [by.get(f["key"], f["label"]) for f in fields]
    text_fields = [f for f in fields if f["type"] != "select"]
    enter_parts = [enter.get(f["key"], f["label"]) for f in text_fields]
    has_select = len(text_fields) < len(fields)
    return (
        _join(by_parts, lang, templates),
        _join(enter_parts, lang, templates) if enter_parts else "",
        len(enter_parts),
        has_select,
    )


# ----------------------------------------------------------------------
# Карточка целиком
# ----------------------------------------------------------------------


def _render_lines(lines: list[str], values: dict[str, str]) -> str:
    """Строки шаблона → текст. Строка, целиком состоящая из пустой
    подстановки ({select_rule} у игр без выбора сервера), выпадает."""
    out = []
    for line in lines:
        rendered = line.format(**values)
        if rendered == "" and line.strip() != "":
            continue
        out.append(rendered)
    return "\n".join(out).strip()


@dataclass
class OfferDraft:
    """Всё для create_offer + запись Listing."""

    payload: dict[str, Any]
    category_id: int
    fees: GGSellFees
    price_rub: Decimal
    price_usd: Decimal
    fz_fields: list[dict[str, Any]] = field(default_factory=list)  # для attach_topup_options


def build_offer(
    position: Position,
    category: dict[str, Any],
    config: PricingConfig,
    *,
    webhook_url: Optional[str],
    fz_fields: Optional[list[dict[str, Any]]] = None,
    markup_percent: Optional[Decimal] = None,
    templates: Optional[dict[str, Any]] = None,
    terms_data: Optional[dict[str, Any]] = None,
) -> OfferDraft:
    """category — запись из data/ggsell_category_map.json (category_id, fee,
    payment_fee). fz_fields — поля покупателя FZ (только у топапов)."""
    templates = templates or load_templates()
    terms_data = terms_data or load_terms()
    source_type = SourceType(position.source_type)  # из БД приходит строкой
    kind = source_type.value  # "topup" / "giftcard"
    if kind not in ("topup", "giftcard"):
        raise ValueError(f"Карточки для {kind} не поддерживаются")
    fz_fields = fz_fields or []
    offer_name = position.raw_payload.get("name", position.name.split(" — ", 1)[-1])
    region = position.region or GLOBAL
    game = game_name(position)
    block = templates[kind]

    texts = {}
    for lang in ("ru", "en"):
        item = format_item(offer_name, source_type=source_type, region=region, lang=lang, terms_data=terms_data)
        # «Xbox Game Pass … Xbox Game Pass Ultimate» → без повтора названия игры.
        if item.casefold().startswith(game.casefold() + " "):
            item = item[len(game) + 1:]
        fields, enter_fields, n_enter, has_select = fields_text(fz_fields, lang, templates)
        rule_key = f"rule_many_fields_{lang}" if n_enter > 1 else f"rule_one_field_{lang}"
        rule = block.get(rule_key, "") if n_enter else ""
        values = {
            "game": game,
            "region": region,
            "item": item,
            "tag": templates["tag"][kind][lang],
            "fields": fields,
            "region_text": region_text(region, source_type, lang, templates),
            "service": game,
        }
        values["rule"] = rule.format(**{**values, "fields": enter_fields}) if rule else ""
        values["select_rule"] = block.get(f"select_rule_{lang}", "") if has_select else ""
        texts[lang] = {
            "title": templates["title"][lang].format(**values),
            "description": _render_lines(block[f"description_{lang}"], values),
            "instructions": block[f"instructions_{lang}"].format(**values),
        }

    fees = GGSellFees.from_category(category.get("fee"), category.get("payment_fee"))
    price = price_rub(position.last_known_price_usd, config, fees, markup_percent)

    payload = {
        "title_ru": texts["ru"]["title"],
        "title_en": texts["en"]["title"],
        "description_ru": texts["ru"]["description"],
        "description_en": texts["en"]["description"],
        "instructions_ru": texts["ru"]["instructions"],
        "instructions_en": texts["en"]["instructions"],
        "cover_image_ru": placeholder_cover_base64(),
        "price": float(price),
        "currency": "RUB",
        "category_id": category["category_id"],
        "is_autoselling": False,
        "min_quantity": 1,
        "max_quantity": 1,
        "is_unlimited_quantity": True,
        # Всегда auto: сменить потом нельзя (notes 3.8); выдача — через чат.
        "delivery": "auto",
    }
    if webhook_url:
        payload["notification_settings"] = {
            "type": "url",
            "url": webhook_url,
            "http_method": "POST",
            "is_disabled": False,
            "is_default": False,
        }
    return OfferDraft(
        payload=payload,
        category_id=category["category_id"],
        fees=fees,
        price_rub=price,
        price_usd=position.last_known_price_usd,
        fz_fields=fz_fields if source_type == SourceType.TOPUP else [],
    )
