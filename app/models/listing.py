"""Лот на GGSell, привязанный к позиции каталога FazerCards.

Обычный лот — один номинал (позиция FZ). Групповой лот (is_group) —
несколько номиналов одной категории FZ через параметр «Номинал»: номиналы —
в listing_variants, position — базовый номинал.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import Optional

from sqlalchemy import ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class ListingStatus(str, enum.Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"
    ON_REVIEW = "on_review"  # проверка модератором GGSell (защищённые категории)


class Listing(TimestampMixin, Base):
    __tablename__ = "listings"

    id: Mapped[int] = mapped_column(primary_key=True)

    position_id: Mapped[int] = mapped_column(ForeignKey("positions.id"))
    position: Mapped["Position"] = relationship(back_populates="listings")  # noqa: F821

    ggsell_offer_id: Mapped[int] = mapped_column(unique=True, index=True)
    status: Mapped[ListingStatus] = mapped_column(String(20), default=ListingStatus.DRAFT)

    # Цена, реально выставленная на GGSell сейчас (в рублях) — то, что видит
    # покупатель. Обновляется модулем sync/ по факту успешного PATCH.
    price_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))

    # Цена поставщика (USD), на основе которой была посчитана price_rub —
    # нужна, чтобы понять, требуется ли пересчёт при следующей синхронизации,
    # не заглядывая каждый раз в Position (она могла обновиться позже).
    price_source_usd_at_sync: Mapped[Decimal] = mapped_column(Numeric(16, 8))

    # Категория GGSell лота и её комиссии на момент выставления (доли: 0.02 =
    # 2%) — удерживаются из цены, поэтому входят в расчёт price_rub
    # (app/pricing/calculator.py, GGSellFees). GGSell меняет комиссии без
    # предупреждения — синхронизация берёт их с витрины (showcase.sync_listings).
    ggsell_category_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    ggsell_fee: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal(0), server_default="0")
    ggsell_payment_fee: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), default=Decimal(0), server_default="0"
    )

    # Индивидуальная наценка лота, % (задаётся из Telegram-панели):
    # пусто — действует глобальная наценка из настроек (app/settings.py).
    markup_percent: Mapped[Optional[Decimal]] = mapped_column(Numeric(7, 2), nullable=True)

    # Опции покупателя повешены на оффер (app/offers/options.py). Лот
    # записывается сразу после create_offer, до опций, — если опции не
    # повесились, повторный автозалив досоздаёт только их.
    options_attached: Mapped[bool] = mapped_column(default=False, server_default="false")

    # Групповой лот: несколько номиналов в одном оффере через параметр
    # «Номинал» (app/models/listing_variant.py). position — базовый (самый
    # дешёвый) номинал, price_rub — его цена.
    is_group: Mapped[bool] = mapped_column(default=False, server_default="false")
    ggsell_option_id: Mapped[Optional[int]] = mapped_column(nullable=True)
    variants: Mapped[list["ListingVariant"]] = relationship(  # noqa: F821
        back_populates="listing", order_by="ListingVariant.sort_order"
    )

    orders: Mapped[list["Order"]] = relationship(back_populates="listing")  # noqa: F821
