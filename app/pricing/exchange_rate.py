"""Курс USD→RUB для расчёта цен.

Решение клиента (01.10): курс ЦБ сам по себе не подходит — доллар
для закупок у FZ обходится дороже ЦБ; брать курс GGSell, если он его отдаёт,
иначе ЦБ + 5%. Проверено 01.10: отдельного курса у GGSell нет,
а цены в USD он пересчитывает ровно по ЦБ (100 000 ₽ = 1196.76 $ → 83.5589
при ЦБ 83.5588) — то есть «курс GGSell» = ЦБ. Поэтому: **курс = ЦБ × (1 +
надбавка)**, надбавка по умолчанию 5% и настраивается (settings).

Источники ЦБ: официальный XML cbr.ru, запасной — зеркало cbr-xml-daily.ru.
Если оба недоступны — остаётся последний сохранённый курс (в настройках).
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app import settings

logger = logging.getLogger(__name__)

CBR_XML_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
CBR_JSON_MIRROR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"


class ExchangeRateError(Exception):
    pass


def parse_cbr_xml(content: bytes) -> Decimal:
    """USD из XML ЦБ: <Valute><CharCode>USD</CharCode><Nominal>1</Nominal>
    <Value>83,5588</Value>. Кодировка windows-1251 объявлена в заголовке."""
    root = ET.fromstring(content)
    for valute in root.iter("Valute"):
        if valute.findtext("CharCode") == "USD":
            value = Decimal(valute.findtext("Value").replace(",", "."))
            nominal = Decimal(valute.findtext("Nominal") or "1")
            return value / nominal
    raise ExchangeRateError("В ответе ЦБ нет курса USD")


def parse_cbr_json(data: dict) -> Decimal:
    usd = data["Valute"]["USD"]
    return Decimal(str(usd["Value"])) / Decimal(str(usd.get("Nominal", 1)))


def fetch_cbr_usd_rub(http_get: Optional[Callable[[str], httpx.Response]] = None) -> Decimal:
    """Курс ЦБ: сначала cbr.ru, при ошибке — зеркало."""
    get = http_get or (lambda url: httpx.get(url, timeout=15, follow_redirects=True))
    errors = []
    try:
        response = get(CBR_XML_URL)
        response.raise_for_status()
        return parse_cbr_xml(response.content)
    except Exception as e:  # noqa: BLE001 — любой сбой источника → пробуем запасной
        errors.append(f"cbr.ru: {e}")
    try:
        response = get(CBR_JSON_MIRROR_URL)
        response.raise_for_status()
        return parse_cbr_json(response.json())
    except Exception as e:  # noqa: BLE001
        errors.append(f"cbr-xml-daily: {e}")
    raise ExchangeRateError("; ".join(errors))


def apply_premium(cbr_rate: Decimal, premium_percent: Decimal) -> Decimal:
    rate = cbr_rate * (Decimal(1) + premium_percent / Decimal(100))
    return rate.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


@dataclass
class RateInfo:
    rate: Decimal  # итоговый курс для цен (ЦБ + надбавка)
    cbr_rate: Optional[Decimal]
    premium_percent: Decimal
    updated_at: Optional[datetime]  # когда курс ЦБ удалось получить последний раз
    fresh: bool  # получен ли курс ЦБ в этот раз (False — взят сохранённый)


def current_rate(session: Session) -> RateInfo:
    """Сохранённый курс без обращения к ЦБ (для расчёта цен и панели)."""
    return RateInfo(
        rate=settings.get_decimal(session, settings.USD_RUB_RATE),
        cbr_rate=settings.get_decimal(session, settings.USD_RUB_CBR),
        premium_percent=settings.get_decimal(session, settings.RATE_PREMIUM_PERCENT),
        updated_at=settings.get_datetime(session, settings.RATE_UPDATED_AT),
        fresh=False,
    )


def refresh_rate(
    session: Session, fetch: Callable[[], Decimal] = fetch_cbr_usd_rub
) -> RateInfo:
    """Получает курс ЦБ, применяет надбавку, сохраняет в настройки. Если ЦБ
    недоступен — оставляет последний сохранённый курс и пишет предупреждение
    (цены продолжают считаться по нему, синхронизация не падает)."""
    premium = settings.get_decimal(session, settings.RATE_PREMIUM_PERCENT)
    try:
        cbr_rate = fetch()
    except ExchangeRateError as e:
        info = current_rate(session)
        logger.warning(
            "Курс ЦБ не получен (%s) — использую сохранённый %s ₽ от %s", e, info.rate, info.updated_at
        )
        return info
    rate = apply_premium(cbr_rate, premium)
    settings.set_value(session, settings.USD_RUB_CBR, cbr_rate)
    settings.set_value(session, settings.USD_RUB_RATE, rate)
    settings.touch(session, settings.RATE_UPDATED_AT)
    logger.info("Курс обновлён: ЦБ %s + %s%% = %s ₽ за $", cbr_rate, premium, rate)
    return RateInfo(
        rate=rate,
        cbr_rate=cbr_rate,
        premium_percent=premium,
        updated_at=settings.get_datetime(session, settings.RATE_UPDATED_AT),
        fresh=True,
    )


def set_premium(session: Session, premium_percent: Decimal) -> RateInfo:
    """Новая надбавка к курсу ЦБ (из панели): итоговый курс пересчитывается
    сразу от последнего сохранённого курса ЦБ, без запроса к ЦБ."""
    if premium_percent < 0 or premium_percent > 100:
        raise ValueError(f"Надбавка к курсу должна быть от 0 до 100%, получено {premium_percent}")
    settings.set_value(session, settings.RATE_PREMIUM_PERCENT, premium_percent)
    cbr_rate = settings.get_decimal(session, settings.USD_RUB_CBR)
    if cbr_rate is not None:
        settings.set_value(session, settings.USD_RUB_RATE, apply_premium(cbr_rate, premium_percent))
    return current_rate(session)
