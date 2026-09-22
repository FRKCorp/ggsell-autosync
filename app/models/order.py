"""Заказ покупателя на GGSell и его прохождение через нашу систему.

Жизненный цикл status (см. architecture-notes.md, раздел про сценарий
"оплатил, а FZ не отвечает"):

    PENDING -> ORDERED_UPSTREAM -> DELIVERED
                    |
                    +-> FAILED -> MANUAL_REVIEW (после исчерпания ретраев)

PRICE_REJECTED — отдельная ветка: цена разошлась с поставщиком больше
MAX_PRICE_DEVIATION_PERCENT на повторной проверке перед списанием — заказ
не отправляется автоматически, уходит сразу в ручной режим.
"""

from __future__ import annotations

import enum
from decimal import Decimal
from typing import Any, Optional

from sqlalchemy import JSON, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class OrderStatus(str, enum.Enum):
    PENDING = "pending"
    PRICE_REJECTED = "price_rejected"
    ORDERED_UPSTREAM = "ordered_upstream"
    DELIVERED = "delivered"
    FAILED = "failed"
    MANUAL_REVIEW = "manual_review"


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)

    # id заказа на стороне GGSell (invoice/id_i из info_order) — то, чем мы
    # оперируем при поллинге/вебхуке и при отправке сообщения в чат.
    ggsell_invoice_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)

    listing_id: Mapped[int] = mapped_column(ForeignKey("listings.id"))
    listing: Mapped["Listing"] = relationship(back_populates="orders")  # noqa: F821

    status: Mapped[OrderStatus] = mapped_column(String(20), default=OrderStatus.PENDING)

    # Данные, введённые покупателем при оформлении (options[].user_data из
    # info_order) — например, invite_url для Steam-гифта или Player ID.
    buyer_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    price_at_sale_rub: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    # Заполняется на шаге повторной проверки цены перед списанием у FZ —
    # NULL, если проверка ещё не выполнялась.
    price_deviation_percent: Mapped[Optional[Decimal]] = mapped_column(
        Numeric(6, 2), nullable=True
    )

    fz_order_id: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)

    retry_count: Mapped[int] = mapped_column(default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # Что реально отправили покупателю в чат — для аудита/разбора споров.
    delivered_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
