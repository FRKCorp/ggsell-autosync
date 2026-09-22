"""Кэш каталога поставщика (FazerCards) — то, что синхронизирует sync/.

Одна строка — одна позиция каталога: категория топапа, карта (giftcard) или
игра (steam gift). Именно к этой таблице привязываются лоты GGSell (Listing).
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class SourceType(str, enum.Enum):
    TOPUP = "topup"
    GIFTCARD = "giftcard"
    STEAM_GIFT = "steam_gift"


class Position(TimestampMixin, Base):
    __tablename__ = "positions"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Канонический ключ для upsert, собирается в sync/ из source_type +
    # идентификаторов ниже (см. app/sync/fz_catalog.py:make_external_id).
    external_id: Mapped[str] = mapped_column(String(300), unique=True, index=True)

    source_type: Mapped[SourceType] = mapped_column(String(20))

    # Идентификаторы у FazerCards различаются по типу источника:
    # - topup: category_id + offer_id (номинал внутри категории)
    # - giftcard: category_id + card_id
    # - steam_gift: appid + sub_id (+ region, т.к. цена зависит от региона)
    fz_category_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fz_offer_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    fz_appid: Mapped[Optional[int]] = mapped_column(nullable=True)
    fz_sub_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    region: Mapped[Optional[str]] = mapped_column(String(10), nullable=True)

    name: Mapped[str] = mapped_column(String(500))

    # Последняя известная цена у поставщика — то, с чем сверяется прайсер.
    last_known_price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4))

    # Сырой ответ API на момент последней синхронизации — на случай, если
    # понадобятся поля, которые мы ещё не выделили в отдельные колонки.
    raw_payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    listings: Mapped[list["Listing"]] = relationship(back_populates="position")

    __table_args__ = ()
