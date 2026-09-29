"""Поля покупателя: FazerCards `fields` ⇄ опции оффера GGSell.

FZ описывает, что нужно от покупателя для заказа топапа, в get_topup_offers:

    "fields": [{"key": "player_id", "label": "Player ID", "type": "text"},
               {"key": "server", "label": "Server", "type": "select",
                "options": [{"label": "Asia", "value": "os_asia"}, ...]}]

Туда (автозалив): каждое поле → опция GGSell. `text` → опция `text`,
`select` → `radio_button` с вариантами без изменения цены. Опции создаются
отдельно от вариантов (create_or_update_options, затем
create_or_update_variants по id опции) — см. attach_topup_options.

Обратно (заказ): в get_order_info приходит options[].{name, user_data} —
название опции, как его видит покупатель, и что он ввёл/выбрал. FZ ждёт
{key: value}, поэтому название переводим обратно в `key`, а подпись варианта
— в `value` (у Wuthering Waves «Asia» → "os_asia"). Без этого заказ у FZ
падает. См. map_buyer_data_to_fz_fields.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.clients.ggsell import GGSellV2Client

# Русские названия опций для типовых подписей FZ. Английская подпись
# остаётся в скобках — покупатель ищет в игре именно её. Подписи, которых
# нет в словаре (Unique ID, Likee ID, Bigo ID), идут как есть.
RU_TITLES = {
    "Player ID": "ID игрока (Player ID)",
    "User ID": "ID пользователя (User ID)",
    "Account ID": "ID аккаунта (Account ID)",
    "Server ID": "ID сервера (Server ID)",
    "Server": "Сервер",
}


# GGSell требует у radio_button ровно один вариант по умолчанию (422 «Для
# варианта с радио-кнопкой должен быть указан 1 вариант по умолчанию»,
# проверено 30 сентября, в документации этого нет). Реальный сервер по
# умолчанию ставить нельзя — покупатель не заметит выбор, и пополнение уйдёт
# на аккаунт с тем же ID на другом сервере. Поэтому по умолчанию стоит
# заглушка: если покупатель её не сменил, заказ уходит в ручной разбор.
PLACEHOLDER_RU = "— выберите сервер —"
PLACEHOLDER_EN = "— choose a server —"


class BuyerDataError(Exception):
    """Данные покупателя из заказа GGSell не удалось сопоставить с полями,
    которые ждёт FazerCards — автоматически заказ отправлять нельзя."""


@dataclass
class OptionSpec:
    """Одна опция GGSell для поля FZ: тело опции + варианты (для radio_button)."""

    fz_key: str
    option: dict[str, Any]
    variants: list[dict[str, Any]] = field(default_factory=list)


def option_titles(fz_field: dict[str, Any]) -> tuple[str, str]:
    label = fz_field["label"]
    return RU_TITLES.get(label, label), label


def build_topup_options(fz_fields: list[dict[str, Any]]) -> list[OptionSpec]:
    """Поля FZ → опции GGSell. Все поля обязательные: без них FZ не
    выполнит пополнение (явный "required" у FZ есть не везде)."""
    specs = []
    for position, fz_field in enumerate(fz_fields):
        title_ru, title_en = option_titles(fz_field)
        is_select = fz_field["type"] == "select"
        option = {
            "type": "radio_button" if is_select else "text",
            "status": "active",
            "title_ru": title_ru,
            "title_en": title_en,
            "comment_ru": (
                "Выберите сервер, на котором находится ваш аккаунт."
                if is_select
                else f"Укажите {fz_field['label']} из профиля в игре. Проверьте перед оплатой — пополнение придёт на этот аккаунт."
            ),
            "comment_en": (
                "Choose the server your account is on."
                if is_select
                else f"Enter your in-game {fz_field['label']}. Double-check it — the top-up goes to this account."
            ),
            "is_required": True,
            "position": position,
        }
        variants = []
        if is_select:
            titles = [(PLACEHOLDER_RU, PLACEHOLDER_EN)] + [
                (choice["label"], choice["label"]) for choice in fz_field.get("options", [])
            ]
            variants = [
                {
                    "title_ru": v_ru,
                    "title_en": v_en,
                    "price": 0,
                    "discount_type": "fixed",
                    "impact_type": "increase",
                    "is_default": i == 0,
                    "status": "active",
                    "position": i,
                }
                for i, (v_ru, v_en) in enumerate(titles)
            ]
        specs.append(OptionSpec(fz_key=fz_field["key"], option=option, variants=variants))
    return specs


def attach_topup_options(
    v2: GGSellV2Client, offer_id: int, fz_fields: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Создаёт опции (и варианты для radio_button) на уже созданном оффере.

    Повторяемая: опции, которые уже есть на оффере (по title_ru), не
    создаются заново, а варианты создаются только у radio_button, где их ещё
    нет — после сбоя посередине можно просто запустить ещё раз. id опций
    берём из list_offer_options, не завязываясь на формат ответа
    create_or_update_options. Возвращает итоговый список опций оффера.
    """
    specs = build_topup_options(fz_fields)
    if not specs:
        return []

    existing = {o["title_ru"]: o for o in _options_list(v2.list_offer_options(offer_id))}
    missing = [s.option for s in specs if s.option["title_ru"] not in existing]
    if missing:
        v2.create_or_update_options(offer_id, missing)

    with_variants = [s for s in specs if s.variants]
    if with_variants:
        if missing:
            existing = {o["title_ru"]: o for o in _options_list(v2.list_offer_options(offer_id))}
        for spec in with_variants:
            created = existing.get(spec.option["title_ru"])
            if created is None:
                raise RuntimeError(
                    f"Опция {spec.option['title_ru']!r} не найдена на оффере {offer_id} после создания"
                )
            if not created.get("variants"):
                v2.create_or_update_variants(offer_id, created["id"], spec.variants)

    return _options_list(v2.list_offer_options(offer_id))


def _options_list(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        return response.get("data", response.get("options", []))
    return response or []


def _norm(value: Any) -> str:
    return " ".join(str(value).split()).casefold()


def map_buyer_data_to_fz_fields(
    fz_fields: list[dict[str, Any]], buyer_data: dict[str, Any]
) -> dict[str, str]:
    """{название опции: что ввёл покупатель} (из get_order_info, см.
    order_processor.extract_buyer_data) → {key: value} для order_topup.

    Опция заказа сопоставляется с полем FZ по названию (русское или
    английское, как их строит build_topup_options; ключ FZ — тоже
    принимаем). Для select подпись варианта переводится в value FZ.
    Бросает BuyerDataError, если какое-то поле FZ не заполнено или выбранный
    вариант не распознан.
    """
    by_name = {
        _norm(name): value for name, value in buyer_data.items() if value not in (None, "")
    }

    result: dict[str, str] = {}
    for fz_field in fz_fields:
        title_ru, title_en = option_titles(fz_field)
        raw_value = next(
            (by_name[n] for n in map(_norm, (title_ru, title_en, fz_field["key"])) if n in by_name),
            None,
        )
        if raw_value is None:
            raise BuyerDataError(f"В заказе не заполнено поле {fz_field['label']!r} ({fz_field['key']})")

        user_value = str(raw_value).strip()
        if fz_field["type"] == "select":
            if _norm(user_value) in (_norm(PLACEHOLDER_RU), _norm(PLACEHOLDER_EN)):
                raise BuyerDataError(f"Покупатель не выбрал {fz_field['label']!r} (осталась заглушка)")
            choice = next(
                (
                    c
                    for c in fz_field.get("options", [])
                    if _norm(user_value) in (_norm(c["label"]), _norm(c["value"]))
                ),
                None,
            )
            if choice is None:
                raise BuyerDataError(
                    f"Вариант {user_value!r} поля {fz_field['label']!r} не найден у FZ"
                )
            result[fz_field["key"]] = choice["value"]
        else:
            result[fz_field["key"]] = user_value
    return result
