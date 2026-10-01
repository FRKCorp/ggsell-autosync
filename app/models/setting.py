"""Настройки, которые меняются во время работы (из Telegram-панели), а не
правкой .env: глобальная наценка, надбавка к курсу ЦБ, текущий курс и т.п.
Ключ → значение строкой; типизация и значения по умолчанию — в app/settings.py.
"""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Setting(TimestampMixin, Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
