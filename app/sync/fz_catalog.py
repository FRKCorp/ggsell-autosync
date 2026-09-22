"""Работа с каталогом FazerCards на уровне БД: точечный импорт конкретных
позиций (пока нет полного списка от клиента) и обновление цен уже
отслеживаемых позиций.

Намеренно НЕ содержит функции "скачать весь каталог" — каталог поставщика
на порядки больше нужных ~2000 позиций (306 категорий топапов, 578
категорий giftcards, 177k+ Steam-игр), см. architecture-notes.md. Это
модуль точечной работы: одна позиция — один вызов import_*/refresh_*.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsClient, FazerCardsError
from app.models.position import Position, SourceType

DEFAULT_STEAM_REGION = "RU"


def make_external_id(
    source_type: SourceType,
    *,
    category_id: Optional[str] = None,
    offer_id: Optional[str] = None,
    appid: Optional[int] = None,
    sub_id: Optional[int] = None,
    region: Optional[str] = None,
) -> str:
    if source_type == SourceType.TOPUP:
        return f"topup:{category_id}:{offer_id}"
    if source_type == SourceType.GIFTCARD:
        return f"giftcard:{category_id}:{offer_id}"
    if source_type == SourceType.STEAM_GIFT:
        return f"steam_gift:{appid}:{sub_id}:{region}"
    raise ValueError(f"Неизвестный source_type: {source_type}")


def _get_or_create(session: Session, external_id: str) -> tuple[Position, bool]:
    existing = session.scalar(select(Position).where(Position.external_id == external_id))
    if existing:
        return existing, False
    position = Position(external_id=external_id)
    session.add(position)
    return position, True


@dataclass
class ImportResult:
    position: Position
    created: bool
    price_changed: bool
    old_price: Optional[Decimal]
    new_price: Decimal


# ----------------------------------------------------------------------
# Импорт конкретной позиции (по явно заданным идентификаторам)
# ----------------------------------------------------------------------


def import_topup(
    session: Session, client: FazerCardsClient, category_id: str, offer_id: str
) -> ImportResult:
    data = client.get_topup_offers(category_id)
    offer = next((o for o in data.get("offers", []) if o.get("offer_id") == offer_id), None)
    if offer is None:
        raise ValueError(f"offer_id={offer_id!r} не найден в категории {category_id!r}")

    external_id = make_external_id(SourceType.TOPUP, category_id=category_id, offer_id=offer_id)
    name = f"{data.get('name', category_id)} — {offer['name']}"
    return _apply_price(
        session,
        external_id=external_id,
        source_type=SourceType.TOPUP,
        name=name,
        price_usd=Decimal(offer["price_usd"]),
        fz_category_id=category_id,
        fz_offer_id=offer_id,
        raw_payload=offer,
    )


def import_giftcard(
    session: Session, client: FazerCardsClient, category_id: str, card_id: str
) -> ImportResult:
    data = client.get_giftcard_offers(category_id)
    offer = next((o for o in data.get("offers", []) if o.get("card_id") == card_id), None)
    if offer is None:
        raise ValueError(f"card_id={card_id!r} не найден в категории {category_id!r}")

    external_id = make_external_id(SourceType.GIFTCARD, category_id=category_id, offer_id=card_id)
    name = f"{data.get('name', category_id)} — {offer['name']}"
    return _apply_price(
        session,
        external_id=external_id,
        source_type=SourceType.GIFTCARD,
        name=name,
        price_usd=Decimal(offer["price_usd"]),
        fz_category_id=category_id,
        fz_offer_id=card_id,
        raw_payload=offer,
    )


def import_steam_gift(
    session: Session,
    client: FazerCardsClient,
    appid: int,
    sub_id: int,
    region: str = DEFAULT_STEAM_REGION,
) -> ImportResult:
    data = client.get_steam_gift_offers(appid)
    offer = next((o for o in data.get("offers", []) if o.get("sub_id") == sub_id), None)
    if offer is None:
        raise ValueError(f"sub_id={sub_id!r} не найден для appid={appid!r}")
    region_price = next(
        (r["price"] for r in offer.get("regions", []) if r.get("region") == region), None
    )
    if region_price is None:
        available = [r.get("region") for r in offer.get("regions", [])]
        raise ValueError(
            f"регион {region!r} недоступен для sub_id={sub_id!r}, доступны: {available}"
        )

    external_id = make_external_id(
        SourceType.STEAM_GIFT, appid=appid, sub_id=sub_id, region=region
    )
    return _apply_price(
        session,
        external_id=external_id,
        source_type=SourceType.STEAM_GIFT,
        name=offer["name"],
        price_usd=Decimal(region_price),
        fz_appid=appid,
        fz_sub_id=sub_id,
        region=region,
        raw_payload=offer,
    )


def _apply_price(
    session: Session,
    *,
    external_id: str,
    source_type: SourceType,
    name: str,
    price_usd: Decimal,
    fz_category_id: Optional[str] = None,
    fz_offer_id: Optional[str] = None,
    fz_appid: Optional[int] = None,
    fz_sub_id: Optional[int] = None,
    region: Optional[str] = None,
    raw_payload: Optional[dict] = None,
) -> ImportResult:
    position, created = _get_or_create(session, external_id)
    old_price = None if created else position.last_known_price_usd

    position.source_type = source_type
    position.name = name
    position.fz_category_id = fz_category_id
    position.fz_offer_id = fz_offer_id
    position.fz_appid = fz_appid
    position.fz_sub_id = fz_sub_id
    position.region = region
    position.last_known_price_usd = price_usd
    position.raw_payload = raw_payload or {}

    session.flush()  # чтобы position.id был доступен сразу после вызова

    price_changed = (not created) and (old_price != price_usd)
    return ImportResult(
        position=position,
        created=created,
        price_changed=price_changed,
        old_price=old_price,
        new_price=price_usd,
    )


# ----------------------------------------------------------------------
# Обновление цены уже отслеживаемой позиции (для периодической sync/-job)
# ----------------------------------------------------------------------


def import_all_topup_offers(
    session: Session,
    client: FazerCardsClient,
    category_id: str,
    region_label: Optional[str] = None,
) -> list["ImportResult"]:
    """Импортирует ВСЕ офферы (номиналы) внутри одной категории
    топапов разом — один вызов get_topup_offers вместо N вызовов import_topup.

    region_label: если у игры несколько вариантов по региону/типу (Free
    Fire, PUBG, MLBB и т.п.), передаём явную пометку — она попадёт в
    название позиции, чтобы не перепутать с другим вариантом той же
    игры (клиент явно попросил такие пометки в названии/описании товара).
    """
    data = client.get_topup_offers(category_id)
    category_name = data.get("name", category_id)
    # FazerCards иногда сам уже включает регион в имя (например, категория
    # mobile_legends_ru отдаёт name="Mobile Legends (RU)") — не дублируем пометку,
    # если она уже в исходном названии.
    if region_label and region_label.lower() not in category_name.lower():
        display_name = f"{category_name} ({region_label})"
    else:
        display_name = category_name

    results = []
    for offer in data.get("offers", []):
        external_id = make_external_id(
            SourceType.TOPUP, category_id=category_id, offer_id=offer["offer_id"]
        )
        name = f"{display_name} — {offer['name']}"
        result = _apply_price(
            session,
            external_id=external_id,
            source_type=SourceType.TOPUP,
            name=name,
            price_usd=Decimal(offer["price_usd"]),
            fz_category_id=category_id,
            fz_offer_id=offer["offer_id"],
            raw_payload=offer,
        )
        results.append(result)
    return results


def refresh_position(session: Session, client: FazerCardsClient, position: Position) -> ImportResult:
    """Повторно запрашивает актуальную цену для одной уже существующей
    Position. ИСПОЛЬЗУЕТСЯ ТОЛЬКО для точечного обновления одной позиции вне
    массовой синхронизации — для всех позиций сразу см. refresh_all_positions,
    которая группирует запросы по категории/appid, а не дёргает API на
    каждую позицию отдельно (иначе одини category_id с N номиналами даёт N
    избыточных запросов к get_topup_offers вместо одного).

    Бросает FazerCardsError, если позиция у поставщика больше не существует —
    вызывающий код решает, что делать (архивировать
    Listing, звать администратора и т.п.), сам этот модуль решения не
    принимает.
    """
    if position.source_type == SourceType.TOPUP:
        return import_topup(session, client, position.fz_category_id, position.fz_offer_id)
    if position.source_type == SourceType.GIFTCARD:
        return import_giftcard(session, client, position.fz_category_id, position.fz_offer_id)
    if position.source_type == SourceType.STEAM_GIFT:
        return import_steam_gift(
            session, client, position.fz_appid, position.fz_sub_id, position.region
        )
    raise ValueError(f"Неизвестный source_type: {position.source_type}")


def _apply_price_to_position(
    session: Session, position: Position, price_usd: Decimal, raw_payload: dict
) -> ImportResult:
    """Как _apply_price, но без get_or_create и пересборки полей идентификации —
    позиция уже известна, меняем только цену/сырой ответ. Используется
    в пакетной refresh_all_positions, где ответ на категорию уже получен одним
    вызовом на множество позиций.
    """
    old_price = position.last_known_price_usd
    position.last_known_price_usd = price_usd
    position.raw_payload = raw_payload
    session.flush()
    return ImportResult(
        position=position,
        created=False,
        price_changed=old_price != price_usd,
        old_price=old_price,
        new_price=price_usd,
    )


def refresh_all_positions(
    session: Session, client: FazerCardsClient
) -> list[tuple[Position, Optional[ImportResult], Optional[str]]]:
    """Обновляет цену ВСЕХ Position в БД, ГРУППИРУЯ запросы к FazerCards по
    категории (topup/giftcard) или appid (steam_gift) — один запрос на группу
    позиций, а не один на каждую позицию (было исправлено после того, как
    на 525 позициях в 37 категориях наивная версия делала 525 запросов
    вместо 37).

    Возвращает список (position, result_или_None, error_или_None) — на
    каждую позицию либо успешный ImportResult, либо текст ошибки (например,
    вся категория или конкретный offer_id больше не существует). Публичная
    сигнатура не изменилась — app/sync/jobs.py править не нужно.
    """
    results: list[tuple[Position, Optional[ImportResult], Optional[str]]] = []
    positions = session.scalars(select(Position)).all()

    topup_groups: dict[str, list[Position]] = {}
    giftcard_groups: dict[str, list[Position]] = {}
    steam_groups: dict[int, list[Position]] = {}

    for p in positions:
        if p.source_type == SourceType.TOPUP:
            topup_groups.setdefault(p.fz_category_id, []).append(p)
        elif p.source_type == SourceType.GIFTCARD:
            giftcard_groups.setdefault(p.fz_category_id, []).append(p)
        elif p.source_type == SourceType.STEAM_GIFT:
            steam_groups.setdefault(p.fz_appid, []).append(p)
        else:
            results.append((p, None, f"Неизвестный source_type: {p.source_type}"))

    for category_id, group in topup_groups.items():
        try:
            data = client.get_topup_offers(category_id)
        except FazerCardsError as e:
            for p in group:
                results.append((p, None, f"FazerCards error {e.status_code}: {e.error}"))
            continue
        offers_by_id = {o["offer_id"]: o for o in data.get("offers", [])}
        for p in group:
            offer = offers_by_id.get(p.fz_offer_id)
            if offer is None:
                results.append(
                    (p, None, f"offer_id={p.fz_offer_id!r} больше не найден в категории {category_id!r}")
                )
                continue
            result = _apply_price_to_position(session, p, Decimal(offer["price_usd"]), offer)
            results.append((p, result, None))

    for category_id, group in giftcard_groups.items():
        try:
            data = client.get_giftcard_offers(category_id)
        except FazerCardsError as e:
            for p in group:
                results.append((p, None, f"FazerCards error {e.status_code}: {e.error}"))
            continue
        offers_by_id = {o["card_id"]: o for o in data.get("offers", [])}
        for p in group:
            offer = offers_by_id.get(p.fz_offer_id)
            if offer is None:
                results.append(
                    (p, None, f"card_id={p.fz_offer_id!r} больше не найден в категории {category_id!r}")
                )
                continue
            result = _apply_price_to_position(session, p, Decimal(offer["price_usd"]), offer)
            results.append((p, result, None))

    for appid, group in steam_groups.items():
        try:
            data = client.get_steam_gift_offers(appid)
        except FazerCardsError as e:
            for p in group:
                results.append((p, None, f"FazerCards error {e.status_code}: {e.error}"))
            continue
        offers_by_sub_id = {o["sub_id"]: o for o in data.get("offers", [])}
        for p in group:
            offer = offers_by_sub_id.get(p.fz_sub_id)
            if offer is None:
                results.append(
                    (p, None, f"sub_id={p.fz_sub_id!r} больше не найден для appid={appid!r}")
                )
                continue
            region_price = next(
                (r["price"] for r in offer.get("regions", []) if r.get("region") == p.region),
                None,
            )
            if region_price is None:
                results.append(
                    (p, None, f"регион {p.region!r} больше не найден для sub_id={p.fz_sub_id!r}")
                )
                continue
            result = _apply_price_to_position(session, p, Decimal(region_price), offer)
            results.append((p, result, None))

    return results
