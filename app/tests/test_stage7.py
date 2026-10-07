"""Этап 7: пополнение Steam, Telegram Stars и Premium — каталог, цена за
единицу, карточка с калькулятором, заказ у FZ (notes 6.22)."""

from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.clients.fazercards import FazerCardsError
from app.models.base import Base
from app.models.listing import Listing, ListingStatus
from app.models.order import Order, OrderStatus
from app.models.position import Position, SourceType
from app.offers.builder import build_offer
from app.offers.categories import SPECIAL_CATEGORIES
from app.offers.options import buyer_fields_for, map_buyer_data_to_fz_fields
from app.orders.order_processor import normalize_telegram_username, process_new_order
from app.pricing.calculator import PricingConfig
from app.pricing.listing_price import listing_price_rub
from app.sync.fz_catalog import is_gone_error
from app.sync.fz_special import (
    import_special_positions,
    refresh_special_positions,
    steam_topup_specs,
    telegram_premium_specs,
    telegram_stars_specs,
)

CONFIG = PricingConfig(Decimal("89.1774"), Decimal("15"), Decimal("3"), Decimal("5"))
RATES = {"ok": True, "base": "USD", "rates": {"USD": 1, "RUB": 83.581967, "UAH": 45.02459, "KZT": 450.409836},
         "updated_at": "2026-10-05T17:59:52.318Z"}
STARS = {"ok": True, "price_per_star": "0.0152625", "min_amount": 50, "max_amount": 10000}
PREMIUM = {"ok": True, "plans": [{"months": 3, "price_usd": "12.1999"}, {"months": 6, "price_usd": "16.2699"},
                                 {"months": 12, "price_usd": "29.4974"}]}
INVOICE = "54300001"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def fz_client():
    fz = MagicMock()
    fz.get_steam_topup_rates.return_value = RATES
    fz.get_telegram_stars.return_value = STARS
    fz.get_telegram_premium.return_value = PREMIUM
    fz.check_steam_login.return_value = {"ok": True, "can_refill": True}
    done = {"ok": True, "order": {"id": "ord-77", "status": "completed", "cards": None}}
    fz.order_steam_topup.return_value = done
    fz.buy_telegram_stars.return_value = done
    fz.buy_telegram_premium.return_value = done
    return fz


# ----------------------------------------------------------------------
# Каталог
# ----------------------------------------------------------------------


def test_steam_specs_unit_price_and_limits():
    specs = {s.external_id: s for s in steam_topup_specs(RATES)}

    rub = specs["steam_topup:RUB"]
    assert rub.price_usd == Decimal("0.01196430")  # 1 / 83.581967
    assert rub.region == "RU" and rub.fz_category_id == "RUB"
    assert (rub.raw_payload["min_units"], rub.raw_payload["max_units"]) == (50, 15000)
    # USD: минимум FZ $0.10 → не меньше 1 доллара, свой потолок 200 < $1000
    usd = specs["steam_topup:USD"]
    assert usd.price_usd == Decimal("1.00000000")
    assert (usd.raw_payload["min_units"], usd.raw_payload["max_units"]) == (1, 200)
    assert set(specs) == {"steam_topup:RUB", "steam_topup:KZT", "steam_topup:UAH", "steam_topup:USD"}


def test_telegram_specs():
    (stars,) = telegram_stars_specs(STARS)
    assert stars.price_usd == Decimal("0.01526250")
    # FZ разрешает от 50, но GGSell не публикует «Звёзды» дешевле 150 ₽ — минимум 100
    assert (stars.raw_payload["min_units"], stars.raw_payload["max_units"]) == (100, 10000)

    premium = {s.external_id: s for s in telegram_premium_specs(PREMIUM)}
    assert premium["telegram_premium:12"].price_usd == Decimal("29.4974")
    assert premium["telegram_premium:3"].fz_offer_id == "3"


def test_every_special_position_has_category():
    specs = steam_topup_specs(RATES) + telegram_stars_specs(STARS) + telegram_premium_specs(PREMIUM)
    assert {s.external_id for s in specs} <= set(SPECIAL_CATEGORIES)


def test_import_and_refresh(session):
    fz = fz_client()
    results = import_special_positions(session, fz)
    assert len(results) == 8 and all(r.created for r in results)

    fz.get_steam_topup_rates.return_value = {**RATES, "rates": {**RATES["rates"], "RUB": 90}}
    fz.get_telegram_premium.return_value = {"ok": True, "plans": PREMIUM["plans"][:2]}  # 12 мес. пропал
    positions = session.scalars(select(Position)).all()

    refreshed = {p.external_id: (r, e) for p, r, e in refresh_special_positions(session, fz, positions)}

    result, error = refreshed["steam_topup:RUB"]
    assert error is None and result.price_changed and result.new_price == Decimal("0.01111111")
    assert is_gone_error(refreshed["telegram_premium:12"][1])

    fz.get_telegram_stars.side_effect = FazerCardsError(503, {"error": "unavailable"})
    stars = [p for p in positions if p.external_id == "telegram_stars:stars"]
    (_, _, error), = refresh_special_positions(session, fz, stars)
    assert error.startswith("FazerCards error 503") and not is_gone_error(error)


# ----------------------------------------------------------------------
# Цена за единицу и карточка
# ----------------------------------------------------------------------


def steam_position(session, currency="RUB"):
    import_special_positions(session, fz_client(), [SourceType.STEAM_TOPUP])
    return session.scalar(select(Position).where(Position.external_id == f"steam_topup:{currency}"))


def test_unit_price_rounded_to_kopeck_not_ruble(session):
    position = steam_position(session)
    listing = Listing(position=position, ggsell_offer_id=1, price_rub=Decimal(0),
                      price_source_usd_at_sync=position.last_known_price_usd,
                      ggsell_fee=Decimal("0.045"), ggsell_payment_fee=Decimal("0.027"))

    price = listing_price_rub(listing, CONFIG)

    # 0.0119643 × 89.1774 × 1.15 / (1 − 0.072) = 1.3222… → вверх до копейки
    assert price == Decimal("1.33")


def test_steam_offer_has_calculator_and_login_field(session):
    position = steam_position(session)

    draft = build_offer(position, SPECIAL_CATEGORIES["steam_topup:RUB"], CONFIG,
                        webhook_url="https://example.test/webhooks/ggsell", fz_fields=buyer_fields_for(position.source_type))

    payload = draft.payload
    assert payload["title_ru"] == "Steam RU* Пополнение баланса (RUB) | по логину | Автодоставка"
    assert payload["price"] == 1.33
    assert (payload["min_quantity"], payload["max_quantity"]) == (50, 15000)
    assert payload["category_id"] == 28831
    assert "от 50 до 15000 RUB" in payload["description_ru"]
    assert "неверно указанный логин Steam" in payload["description_ru"]
    assert draft.fz_fields == [{"key": "steam_login", "label": "Steam login", "type": "text"}]


def test_premium_offer_is_fixed_price(session):
    import_special_positions(session, fz_client(), [SourceType.TELEGRAM_PREMIUM])
    position = session.scalar(select(Position).where(Position.external_id == "telegram_premium:3"))

    draft = build_offer(position, SPECIAL_CATEGORIES["telegram_premium:3"], CONFIG,
                        webhook_url=None, fz_fields=buyer_fields_for(position.source_type))

    assert draft.payload["title_ru"] == "Telegram Global* Premium на 3 мес. | по username | Автодоставка"
    assert (draft.payload["min_quantity"], draft.payload["max_quantity"]) == (1, 1)
    assert draft.payload["price"] == float(draft.price_rub) and draft.price_rub == draft.price_rub.to_integral()


def test_buyer_data_maps_to_special_fields():
    fields = buyer_fields_for(SourceType.STEAM_TOPUP)
    assert map_buyer_data_to_fz_fields(fields, {"Логин Steam (Steam login)": " gaben "}) == {"steam_login": "gaben"}


@pytest.mark.parametrize("raw", ["durov", "@durov", "t.me/durov", "https://t.me/durov/", " @durov "])
def test_normalize_telegram_username(raw):
    assert normalize_telegram_username(raw) == "@durov"


# ----------------------------------------------------------------------
# Заказ
# ----------------------------------------------------------------------


def make_listing(session, external_id, kinds):
    import_special_positions(session, fz_client(), kinds)
    position = session.scalar(select(Position).where(Position.external_id == external_id))
    category = SPECIAL_CATEGORIES[external_id]
    listing = Listing(position=position, ggsell_offer_id=777, status=ListingStatus.ACTIVE,
                      price_rub=Decimal("1.33"), price_source_usd_at_sync=position.last_known_price_usd,
                      ggsell_fee=Decimal(str(category["fee"])), ggsell_payment_fee=Decimal("0.027"))
    session.add(listing)
    session.commit()
    return listing


def order_info(option_name, value, cnt_goods="100.0", amount=133.0):
    return {"content": {"item_id": 777, "amount": amount, "profit": 123.4, "cnt_goods": cnt_goods,
                        "date_pay": "2026-10-05T20:45:38+03:00",
                        "options": [{"id": 1, "name": option_name, "user_data": value}]}}


def run(session, fz, info):
    v1 = MagicMock()
    v1.get_order_info.return_value = info
    with patch("app.orders.order_processor.notify_admin") as alert:
        order = process_new_order(session, fz, v1, CONFIG, INVOICE, sleep=lambda s: None)
    return order, v1, alert


def test_steam_order_end_to_end(session):
    make_listing(session, "steam_topup:RUB", [SourceType.STEAM_TOPUP])
    fz = fz_client()

    order, v1, alert = run(session, fz, order_info("Логин Steam (Steam login)", "gaben"))

    fz.check_steam_login.assert_called_once_with("gaben")
    fz.order_steam_topup.assert_called_once_with("gaben", "RUB", 100, idempotency_key=INVOICE)
    assert order.status == OrderStatus.DELIVERED and order.quantity == 100
    message = v1.create_message.call_args.args[1]
    assert "Баланс Steam пополнен на 100 RUB" in message and "gaben" in message
    alert.assert_not_called()


def test_steam_login_that_cannot_be_refilled_goes_to_manual_review(session):
    make_listing(session, "steam_topup:RUB", [SourceType.STEAM_TOPUP])
    fz = fz_client()
    fz.check_steam_login.return_value = {"ok": True, "can_refill": False}

    order, _, alert = run(session, fz, order_info("Логин Steam (Steam login)", "typo_login"))

    fz.order_steam_topup.assert_not_called()
    assert order.status == OrderStatus.MANUAL_REVIEW and "нельзя пополнить" in order.error_message
    assert "Заказ у FZ НЕ сделан" in alert.call_args.args[0]


def test_quantity_outside_limits_goes_to_manual_review(session):
    make_listing(session, "steam_topup:RUB", [SourceType.STEAM_TOPUP])
    fz = fz_client()

    order, _, _ = run(session, fz, order_info("Логин Steam (Steam login)", "gaben", cnt_goods="20000.0"))

    fz.order_steam_topup.assert_not_called()
    assert order.status == OrderStatus.MANUAL_REVIEW and "вне диапазона 50–15000" in order.error_message


def test_stars_order_normalizes_username(session):
    make_listing(session, "telegram_stars:stars", [SourceType.TELEGRAM_STARS])
    fz = fz_client()

    order, v1, _ = run(session, fz, order_info("Username Telegram", "t.me/durov", cnt_goods="250.0"))

    fz.buy_telegram_stars.assert_called_once_with("@durov", 250, idempotency_key=INVOICE)
    assert order.status == OrderStatus.DELIVERED
    assert "(250 шт.)" in v1.create_message.call_args.args[1]


def test_telegram_not_retried_after_network_failure(session):
    make_listing(session, "telegram_stars:stars", [SourceType.TELEGRAM_STARS])
    fz = fz_client()
    fz.buy_telegram_stars.side_effect = httpx.ConnectError("connection reset")

    order, _, alert = run(session, fz, order_info("Username Telegram", "durov", cnt_goods="100.0"))

    assert fz.buy_telegram_stars.call_count == 1  # без повтора — мог купить второй раз
    assert order.status == OrderStatus.MANUAL_REVIEW
    assert "МОГ пройти" in alert.call_args.args[0]


def test_steam_retried_after_network_failure_with_same_key(session):
    make_listing(session, "steam_topup:RUB", [SourceType.STEAM_TOPUP])
    fz = fz_client()
    fz.order_steam_topup.side_effect = [httpx.ConnectError("reset"), fz.order_steam_topup.return_value]

    order, _, _ = run(session, fz, order_info("Логин Steam (Steam login)", "gaben"))

    assert fz.order_steam_topup.call_count == 2  # у Steam Idempotency-Key есть — повтор безопасен
    assert {c.kwargs["idempotency_key"] for c in fz.order_steam_topup.call_args_list} == {INVOICE}
    assert order.status == OrderStatus.DELIVERED


def test_premium_order(session):
    make_listing(session, "telegram_premium:6", [SourceType.TELEGRAM_PREMIUM])
    fz = fz_client()

    order, v1, _ = run(session, fz, order_info("Username Telegram", "@durov", cnt_goods="1.0", amount=2000.0))

    fz.buy_telegram_premium.assert_called_once_with("@durov", 6, idempotency_key=INVOICE)
    assert order.quantity == 1
    assert "Telegram Premium на 6 мес. оформлен" in v1.create_message.call_args.args[1]
