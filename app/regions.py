"""Регионы товаров: коды, названия и определение региона позиции.

Требование клиента (01.10, notes 6.11): регион — на каждом товаре, в названии
и описании. Откуда берём (roadmap 1.9), по приоритету:
  1. из названия номинала FZ — «1 Month (UK) Standard» у CapCut;
  2. из конфига категории (`region` в data/selected_*_categories.json);
  3. из примечания категории FZ — строка «Region: India» в поле `note`
     (на сайте FZ это «оранжевое окошко под аватаркой услуги»);
  4. Global — если о регионе нигде не сказано.

Регион хранится кодом: US, RU, EU, CIS, Global, MENA…
"""

from __future__ import annotations

import re
from typing import Optional

GLOBAL = "Global"

# Код региона → как он называется в категориях GGSell и примечаниях FZ (по-русски
# и по-английски встречаются оба варианта). Порядок важен для подбора
# категории GGSell (app/offers/categories.py): сначала точная страна, потом
# более общий регион (для еврозоны — «Европа»).
REGION_NAMES: dict[str, list[str]] = {
    "AE": ["ОАЭ", "UAE", "Emirates", "United Arab Emirates"],
    "AR": ["Аргентина", "Argentina"],
    "AT": ["Австрия", "Austria", "Европа", "Europe"],
    "AU": ["Австралия", "Australia"],
    "BE": ["Бельгия", "Belgium", "Европа", "Europe"],
    "BH": ["Бахрейн", "Bahrain"],
    "BR": ["Бразилия", "Brazil"],
    "BY": ["Беларусь", "Belarus"],
    "CA": ["Канада", "Canada"],
    "CH": ["Швейцария", "Switzerland"],
    "CIS": ["Страны СНГ", "СНГ", "CIS"],
    "CL": ["Чили", "Chile"],
    "CN": ["Китай", "China"],
    "CO": ["Колумбия", "Colombia"],
    "CR": ["Коста-Рика", "Costa Rica"],
    "CZ": ["Чехия", "Чешская Республика", "Czech", "Czech Republic"],
    "DE": ["Германия", "Germany", "Европа", "Europe"],
    "DK": ["Дания", "Denmark"],
    "ES": ["Испания", "Spain", "Европа", "Europe"],
    "EU": ["Европа", "Europe"],
    "FI": ["Финляндия", "Finland", "Европа", "Europe"],
    "FR": ["Франция", "France", "Европа", "Europe"],
    "GLOBAL": ["Global", "Глобал"],
    "HK": ["Гонконг", "Hong Kong"],
    "HR": ["Хорватия", "Croatia"],
    "HU": ["Венгрия", "Hungary"],
    "ID": ["Индонезия", "Indonesia"],
    "IE": ["Ирландия", "Ireland", "Европа", "Europe"],
    "IN": ["Индия", "India"],
    "IT": ["Италия", "Italy", "Европа", "Europe"],
    "JP": ["Япония", "Japan"],
    "KR": ["Южная Корея", "South Korea", "Korea", "Корея"],
    "KW": ["Кувейт", "Kuwait"],
    "KZ": ["Казахстан", "Kazakhstan"],
    "LB": ["Ливан", "Lebanon"],
    "LU": ["Люксембург", "Luxembourg", "Европа", "Europe"],
    "MX": ["Мексика", "Mexico"],
    "MY": ["Малайзия", "Malaysia"],
    "NA": ["Северная Америка", "North America"],
    "NL": ["Нидерланды", "Netherlands", "Европа", "Europe"],
    "NO": ["Норвегия", "Norway"],
    "NZ": ["Новая Зеландия", "New Zealand"],
    "OM": ["Оман", "Oman"],
    "PE": ["Перу", "Peru"],
    "PH": ["Филиппины", "Philippines"],
    "PL": ["Польша", "Poland"],
    "PT": ["Португалия", "Portugal", "Европа", "Europe"],
    "QA": ["Катар", "Qatar"],
    "RO": ["Румыния", "Romania"],
    "RU": ["Россия", "Russia"],
    "SA": ["Саудовская Аравия", "Saudi", "Saudi Arabia"],
    "SE": ["Швеция", "Sweden"],
    "SG": ["Сингапур", "Singapore"],
    "SK": ["Словакия", "Slovakia"],
    "TH": ["Таиланд", "Тайланд", "Thailand"],
    "TR": ["Турция", "Turkey"],
    "TW": ["Тайвань", "Taiwan"],
    "UA": ["Украина", "Ukraine"],
    "UK": ["Великобритания", "United Kingdom", "UK"],
    "US": ["США", "USA", "United States"],
    "UY": ["Уругвай", "Uruguay"],
    "VN": ["Вьетнам", "Vietnam"],
    "ZA": ["ЮАР", "Южная Африка", "South Africa"],
}

# Коды, которые FZ пишет в скобках прямо в названии номинала («1 Month (UK)
# Standard»). Только в скобках — без них коды вроде «IN», «ID», «PC» слишком
# легко совпадают с обычными словами.
_CODES = sorted(set(REGION_NAMES) - {"GLOBAL"} | {"MENA", "LATAM"}, key=len, reverse=True)
_OFFER_REGION_RE = re.compile(rf"\(\s*({'|'.join(_CODES)}|Global)\s*\)", re.IGNORECASE)
_NOTE_REGION_RE = re.compile(r"Region:\s*([^\n]+)")


def normalize_region(value: Optional[str]) -> Optional[str]:
    """«global»/«GLOBAL» → «Global», коды — в верхний регистр."""
    if not value:
        return None
    value = value.strip()
    return GLOBAL if value.casefold() == "global" else value.upper()


def region_from_offer_name(offer_name: str) -> Optional[str]:
    match = _OFFER_REGION_RE.search(offer_name or "")
    return normalize_region(match.group(1)) if match else None


def region_from_note(note: Optional[str]) -> Optional[str]:
    """«Region: India» → "IN", «Region: Global» → "Global". Несколько регионов
    через «/» («CN / Other / US / VN») или неизвестное название → None."""
    match = _NOTE_REGION_RE.search(note or "")
    if not match:
        return None
    raw = match.group(1).strip()
    if "/" in raw or "," in raw:
        return None
    if raw.casefold() == "global":
        return GLOBAL
    if raw.upper() in REGION_NAMES or raw.upper() in {"MENA", "LATAM"}:
        return raw.upper()
    for code, names in REGION_NAMES.items():
        # Только «собственное» название страны (до общего «Европа» в списке).
        own = names[: names.index("Европа")] if "Европа" in names and code != "EU" else names
        if any(raw.casefold() == n.casefold() for n in own):
            return normalize_region(code)
    return None


def resolve_region(
    offer_name: str, config_region: Optional[str] = None, category_note: Optional[str] = None
) -> str:
    """Регион позиции по приоритету: номинал → конфиг → примечание FZ → Global."""
    return (
        region_from_offer_name(offer_name)
        or normalize_region(config_region)
        or region_from_note(category_note)
        or GLOBAL
    )
