"""Настройки прайсера в БД (таблица settings, app/models/setting.py).

Клиент меняет их из Telegram-панели «Авто-прайсер» (roadmap 6.8), поэтому
они живут в БД, а не в .env. Значения из .env — только начальные: пока
ключа в БД нет, берётся значение по умолчанию из окружения.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from dotenv import load_dotenv
from sqlalchemy.orm import Session

from app.models.setting import Setting

# Ключи
GLOBAL_MARKUP_PERCENT = "global_markup_percent"  # массовая наценка, % (решение клиента 2.5)
RATE_PREMIUM_PERCENT = "rate_premium_percent"  # надбавка к курсу ЦБ, % (по умолчанию 5)
USD_RUB_CBR = "usd_rub_cbr"  # последний полученный курс ЦБ
USD_RUB_RATE = "usd_rub_rate"  # итоговый курс для расчёта цен (ЦБ + надбавка)
RATE_UPDATED_AT = "rate_updated_at"  # когда курс ЦБ последний раз удалось получить
PRICES_UPDATED_AT = "prices_updated_at"  # последнее обновление цен (для панели)
SYNC_FINISHED_AT = "sync_finished_at"  # синхронизация цен дошла до конца (сторож, 8.4)
PRICER_ENABLED = "pricer_enabled"  # выключатель прайсера: false — цены на витрину не отправляются (6.8)
# «Обновить цены СЕЙЧАС» из бота (6.8): бот ставит запрос, scheduler его
# забирает и запускает ту же джобу refresh_prices (две синхронизации разом не
# идут — max_instances=1), по окончании присылает итог в Telegram.
PRICE_SYNC_REQUESTED_AT = "price_sync_requested_at"
PRICE_SYNC_REPORT_PENDING = "price_sync_report_pending"

_ENV_DEFAULTS = {
    GLOBAL_MARKUP_PERCENT: ("MARKUP_PERCENT", "15.0"),
    RATE_PREMIUM_PERCENT: ("EXCHANGE_RATE_PREMIUM_PERCENT", "5.0"),
    # Запасной курс — только если ЦБ ни разу не удалось получить.
    USD_RUB_RATE: ("EXCHANGE_RATE_USD_TO_RUB", "95.0"),
    PRICER_ENABLED: ("PRICER_ENABLED", "true"),
}


def get(session: Session, key: str) -> Optional[str]:
    row = session.get(Setting, key)
    if row is not None:
        return row.value
    if key in _ENV_DEFAULTS:
        load_dotenv()
        env_key, default = _ENV_DEFAULTS[key]
        return os.getenv(env_key, default)
    return None


def set_value(session: Session, key: str, value: object) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=str(value)))
    else:
        row.value = str(value)
    session.flush()


def get_decimal(session: Session, key: str) -> Optional[Decimal]:
    value = get(session, key)
    return Decimal(value) if value is not None else None


def get_datetime(session: Session, key: str) -> Optional[datetime]:
    value = get(session, key)
    return datetime.fromisoformat(value) if value else None


def touch(session: Session, key: str) -> None:
    set_value(session, key, datetime.now(timezone.utc).isoformat())


def global_markup_percent(session: Session) -> Decimal:
    return get_decimal(session, GLOBAL_MARKUP_PERCENT)


def set_global_markup_percent(session: Session, percent: Decimal) -> None:
    if percent < 0 or percent > 1000:
        raise ValueError(f"Наценка должна быть от 0 до 1000%, получено {percent}")
    set_value(session, GLOBAL_MARKUP_PERCENT, percent)


def get_bool(session: Session, key: str) -> bool:
    return str(get(session, key)).strip().lower() in ("1", "true", "yes", "on")


def pricer_enabled(session: Session) -> bool:
    return get_bool(session, PRICER_ENABLED)


def set_pricer_enabled(session: Session, enabled: bool) -> None:
    set_value(session, PRICER_ENABLED, "true" if enabled else "false")


def request_price_sync(session: Session) -> None:
    touch(session, PRICE_SYNC_REQUESTED_AT)


def take_price_sync_request(session: Session) -> bool:
    """True, если из бота запросили синхронизацию; запрос снимается, а
    итог синхронизации будет отправлен в Telegram."""
    if not get(session, PRICE_SYNC_REQUESTED_AT):
        return False
    set_value(session, PRICE_SYNC_REQUESTED_AT, "")
    set_value(session, PRICE_SYNC_REPORT_PENDING, "true")
    return True


def take_price_sync_report(session: Session) -> bool:
    """True один раз после синхронизации, запрошенной из бота."""
    if not get_bool(session, PRICE_SYNC_REPORT_PENDING):
        return False
    set_value(session, PRICE_SYNC_REPORT_PENDING, "false")
    return True
