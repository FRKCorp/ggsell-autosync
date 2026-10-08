"""Позиции этапа 7 — пополнение Steam, Telegram Stars и Premium.

Это не каталог /topups, а отдельные эндпоинты FZ без номиналов:
  - Steam: курс валюты кошелька к USD (/steam-topup/rates), сумма — любая
    от $0.10 до $1000 в эквиваленте;
  - Stars: цена одной звезды и диапазон количества (/telegram/stars);
  - Premium: три фиксированных плана 3/6/12 месяцев (/telegram/premium).

Поэтому позиции строим сами. Steam и Stars — «цена за единицу»
(UNIT_PRICED): last_known_price_usd — цена одной единицы (1 ₽ Steam, 1
звезда), лот на GGSell продаётся калькулятором, количество выбирает
покупатель в пределах min_units..max_units (raw_payload). Premium — обычные
фиксированные позиции, как номиналы топапов.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsClient, FazerCardsError
from app.models.position import Position, SourceType
from app.regions import GLOBAL
from app.sync.fz_catalog import GONE_MARKER, ImportResult, _apply_price

# Лимиты FZ на одно пополнение Steam — в эквиваленте USD (документация FZ,
# виджет Steam top-up: «минимум всегда эквивалент 0,10 USD, максимум 1 000 USD»).
FZ_STEAM_MIN_USD = Decimal("0.10")
FZ_STEAM_MAX_USD = Decimal("1000")

# Валюты кошелька, которые продаём (клиент: RUB / KZT / UAH, плюс USD для СНГ —
# так делают конкуренты), регион для названия и рамки калькулятора.
# Рамки — свои, внутри лимитов FZ: слишком крупное разовое пополнение —
# лишний риск (конкуренты советуют не больше 10 000 ₽ за раз). Поменять — здесь.
STEAM_TOPUP_CURRENCIES: dict[str, dict[str, Any]] = {
    "RUB": {"region": "RU", "min_units": 50, "max_units": 15000},
    "KZT": {"region": "KZ", "min_units": 250, "max_units": 75000},
    "UAH": {"region": "UA", "min_units": 25, "max_units": 7500},
    "USD": {"region": "CIS", "min_units": 1, "max_units": 200},
}
PREMIUM_MONTHS = (3, 6, 12)

# GGSell: у категории «Telegram > Звезды» минимальная цена заказа 150 ₽ — с
# меньшим min_quantity оффер не публикуется («Offer cannot be activate»).
# Поддержка GGSell 07.10: min_quantity = 100 (100 × ~1.7 ₽ ≈ 174 ₽).
STARS_MIN_UNITS = 100

UNIT_PRICE_PLACES = Decimal("0.00000001")  # как Numeric(16, 8) в Position


def _unit_price(value: Decimal) -> Decimal:
    return value.quantize(UNIT_PRICE_PLACES, rounding=ROUND_HALF_UP)


@dataclass
class SpecialSpec:
    """Что записать в Position (kwargs для _apply_price)."""

    external_id: str
    source_type: SourceType
    name: str
    price_usd: Decimal
    fz_category_id: Optional[str]
    fz_offer_id: Optional[str]
    region: str
    raw_payload: dict[str, Any] = field(default_factory=dict)


def steam_topup_specs(rates_response: dict[str, Any]) -> list[SpecialSpec]:
    rates = rates_response.get("rates", {})
    specs = []
    for currency, cfg in STEAM_TOPUP_CURRENCIES.items():
        rate = rates.get(currency)
        if not rate:
            continue
        rate = Decimal(str(rate))
        fz_min = math.ceil(FZ_STEAM_MIN_USD * rate)
        fz_max = math.floor(FZ_STEAM_MAX_USD * rate)
        specs.append(SpecialSpec(
            external_id=f"steam_topup:{currency}",
            source_type=SourceType.STEAM_TOPUP,
            name=f"Steam ({cfg['region']}) — Wallet top-up {currency}",
            price_usd=_unit_price(Decimal(1) / rate),
            fz_category_id=currency,
            fz_offer_id=None,
            region=cfg["region"],
            raw_payload={
                "name": f"Wallet top-up {currency}",
                "item_ru": f"Пополнение баланса ({currency})",
                "item_en": f"Wallet top-up ({currency})",
                "unit": currency,
                "rate": str(rate),
                "min_units": max(cfg["min_units"], fz_min),
                "max_units": min(cfg["max_units"], fz_max),
                "updated_at": rates_response.get("updated_at"),
            },
        ))
    return specs


def telegram_stars_specs(stars_response: dict[str, Any]) -> list[SpecialSpec]:
    return [SpecialSpec(
        external_id="telegram_stars:stars",
        source_type=SourceType.TELEGRAM_STARS,
        name="Telegram (Global) — Stars",
        price_usd=_unit_price(Decimal(str(stars_response["price_per_star"]))),
        fz_category_id="stars",
        fz_offer_id=None,
        region=GLOBAL,
        raw_payload={
            "name": "Stars",
            "item_ru": "Звёзды",
            "item_en": "Stars",
            "unit": "stars",
            "min_units": max(int(stars_response.get("min_amount", 50)), STARS_MIN_UNITS),
            "max_units": int(stars_response.get("max_amount", 10000)),
            "updated_at": stars_response.get("rates_updated_at"),
        },
    )]


def telegram_premium_specs(premium_response: dict[str, Any]) -> list[SpecialSpec]:
    plans = {int(p["months"]): Decimal(str(p["price_usd"])) for p in premium_response.get("plans", [])}
    return [
        SpecialSpec(
            external_id=f"telegram_premium:{months}",
            source_type=SourceType.TELEGRAM_PREMIUM,
            name=f"Telegram (Global) — Premium {months} months",
            price_usd=plans[months],
            fz_category_id="premium",
            fz_offer_id=str(months),
            region=GLOBAL,
            raw_payload={
                "name": f"Premium {months} months",
                "item_ru": f"Premium на {months} мес.",
                "item_en": f"Premium for {months} months",
                "months": months,
            },
        )
        for months in PREMIUM_MONTHS
        if months in plans
    ]


# Тип позиции → (запрос к FZ, разбор ответа в позиции)
FETCHERS: dict[SourceType, tuple[Callable[[FazerCardsClient], dict], Callable[[dict], list[SpecialSpec]]]] = {
    SourceType.STEAM_TOPUP: (lambda fz: fz.get_steam_topup_rates(), steam_topup_specs),
    SourceType.TELEGRAM_STARS: (lambda fz: fz.get_telegram_stars(), telegram_stars_specs),
    SourceType.TELEGRAM_PREMIUM: (lambda fz: fz.get_telegram_premium(), telegram_premium_specs),
}


def fetch_specs(fz: FazerCardsClient, source_type: SourceType) -> list[SpecialSpec]:
    request, parse = FETCHERS[source_type]
    return parse(request(fz))


def _apply_spec(session: Session, spec: SpecialSpec) -> ImportResult:
    return _apply_price(
        session,
        external_id=spec.external_id,
        source_type=spec.source_type,
        name=spec.name,
        price_usd=spec.price_usd,
        fz_category_id=spec.fz_category_id,
        fz_offer_id=spec.fz_offer_id,
        region=spec.region,
        raw_payload=spec.raw_payload,
    )


def import_special_positions(
    session: Session, fz: FazerCardsClient, source_types: Optional[list[SourceType]] = None
) -> list[ImportResult]:
    """Создать/обновить позиции этапа 7 (scripts/import_special.py)."""
    results = []
    for source_type in source_types or list(FETCHERS):
        for spec in fetch_specs(fz, source_type):
            results.append(_apply_spec(session, spec))
    session.commit()
    return results


def refresh_special_positions(
    session: Session, fz: FazerCardsClient, positions: list[Position]
) -> list[tuple[Position, Optional[ImportResult], Optional[str]]]:
    """Для refresh_all_positions: один запрос к FZ на тип, ошибки — в формате
    остальных позиций (is_gone_error понимает «больше не найден»)."""
    results: list[tuple[Position, Optional[ImportResult], Optional[str]]] = []
    by_type: dict[SourceType, list[Position]] = {}
    for p in positions:
        by_type.setdefault(SourceType(p.source_type), []).append(p)
    for source_type, group in by_type.items():
        try:
            specs = {s.external_id: s for s in fetch_specs(fz, source_type)}
        except FazerCardsError as e:
            results.extend((p, None, f"FazerCards error {e.status_code}: {e.error}") for p in group)
            continue
        for p in group:
            spec = specs.get(p.external_id)
            if spec is None:
                results.append((p, None, f"{p.external_id} {GONE_MARKER} в ответе FZ"))
                continue
            results.append((p, _apply_spec(session, spec), None))
    return results
