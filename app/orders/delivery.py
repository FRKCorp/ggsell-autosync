"""Сообщение покупателю после выполнения заказа у FZ (roadmap 5.3).

Тексты — data/offer_templates.json, раздел "delivery". Коды подарочных
карт FZ отдаёт в order.cards (Cookbook FZ: «мгновенные товары возвращаются
со статусом completed и кодами в order.cards», то же — в GET /orders/{id}).
Точный формат элемента cards на живом заказе ещё не видели (roadmap 5.1),
поэтому разбор терпимый: строка — это код; словарь — берём известные поля.
Нет ни одного кода — None: такой заказ уходит в ручной разбор, сырой ответ
FZ покупателю не отправляем.
"""

from __future__ import annotations

from typing import Any, Optional

from app.models.position import Position, SourceType
from app.offers.builder import format_item, game_name, load_templates

# Поля элемента cards, похожие на код, и как их подписать покупателю.
_CODE_FIELDS = [
    ("code", "Код"),
    ("card_code", "Код"),
    ("redeem_code", "Код"),
    ("key", "Ключ"),
    ("card_number", "Номер карты"),
    ("serial", "Серийный номер"),
    ("pin", "PIN"),
    ("password", "Пароль"),
]


def extract_codes(fz_order: dict[str, Any]) -> list[str]:
    """Коды из ответа FZ (order или обёртка {"order": ...}) — по строке на карту."""
    order = fz_order.get("order", fz_order)
    cards = order.get("cards") or order.get("codes") or []
    if isinstance(cards, (str, dict)):
        cards = [cards]
    lines = []
    for card in cards:
        if isinstance(card, str) and card.strip():
            lines.append(card.strip())
        elif isinstance(card, dict):
            parts = [
                f"{label}: {card[key]}" if len(card) > 1 else str(card[key])
                for key, label in _CODE_FIELDS
                if isinstance(card.get(key), (str, int)) and str(card[key]).strip()
            ]
            if parts:
                lines.append(", ".join(parts))
    return lines


def format_delivery_message(
    position: Position, buyer_data: dict[str, Any], fz_order: dict[str, Any], quantity: int = 1
) -> Optional[str]:
    templates = load_templates()["delivery"]
    source_type = SourceType(position.source_type)
    raw = position.raw_payload or {}
    offer_name = raw.get("name", position.name.split(" — ", 1)[-1])
    values = {
        "item": raw.get("item_ru") or format_item(offer_name, source_type=source_type, region=position.region, lang="ru"),
        "service": game_name(position),
        "account": ", ".join(f"{name}: {value}" for name, value in buyer_data.items()) or "—",
        "codes": "",
        "quantity": quantity,
        "unit": raw.get("unit", ""),
    }
    if source_type == SourceType.GIFTCARD:
        codes = extract_codes(fz_order)
        if not codes:
            return None
        values["codes"] = "\n".join(codes)
        lines = templates["giftcard"]
    else:
        # Этап 7 — свои тексты (Steam / Stars / Premium), остальное — пополнение.
        lines = templates.get(source_type.value, templates["topup"])
    return "\n".join(line.format(**values) for line in lines).strip()
