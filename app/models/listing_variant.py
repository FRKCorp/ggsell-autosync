"""Номинал группового лота: вариант параметра «Номинал» на GGSell, привязанный
к позиции FazerCards.

Групповой лот (Listing.is_group) — один оффер на несколько номиналов одной
категории FZ (App Store по регионам): базовая цена лота — цена самого
дешёвого номинала, у остальных вариантов надбавка = их цена − базовая.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ListingVariant(TimestampMixin, Base):
    __tablename__ = "listing_variants"

    id: Mapped[int] = mapped_column(primary_key=True)

    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"), index=True)
    listing: Mapped["Listing"] = relationship(back_populates="variants")  # noqa: F821

    # Позиция FZ входит не больше чем в один групповой лот.
    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"), unique=True)
    position: Mapped["Position"] = relationship()  # noqa: F821

    # id варианта на GGSell — по нему в заказе определяется купленный номинал
    # (options[].user_data_id). Пусто, пока вариант не создан на GGSell.
    ggsell_variant_id: Mapped[Optional[int]] = mapped_column(unique=True, nullable=True)

    # Полная цена номинала на витрине (база лота + надбавка варианта) и цена
    # поставщика, из которой она посчитана.
    price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    price_source_usd_at_sync: Mapped[Decimal] = mapped_column(Numeric(16, 8))

    # Порядок в списке номиналов (по возрастанию цены).
    sort_order: Mapped[int] = mapped_column(default=0, server_default="0")

    # active — продаётся; hidden — скрыт на GGSell (номинал убран из группы).
    status: Mapped[str] = mapped_column(String(20), default="active", server_default="active")
