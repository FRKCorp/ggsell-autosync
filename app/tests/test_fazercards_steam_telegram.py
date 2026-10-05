import json

import httpx
import pytest
import respx

from app.clients.fazercards import FazerCardsClient, FazerCardsError

BASE = "https://fz.test/api/v2"
ORDER = {"ok": True, "order": {"id": "ord-1", "kind": "gift_card", "status": "processing"}}


@pytest.fixture
def fz():
    with FazerCardsClient(api_key="key", base_url=BASE) as client:
        yield client


def body(route):
    return json.loads(route.calls.last.request.content)


@respx.mock
def test_steam_topup_rates_and_check_login(fz):
    respx.get(f"{BASE}/steam-topup/rates").respond(json={"ok": True, "base": "USD", "rates": {"RUB": 83.58}})
    check = respx.post(f"{BASE}/steam-topup/check-login").respond(json={"ok": True, "can_refill": True})

    assert fz.get_steam_topup_rates()["rates"]["RUB"] == 83.58
    assert fz.check_steam_login("gaben")["can_refill"] is True
    assert body(check) == {"steamLogin": "gaben"}
    assert check.calls.last.request.headers["X-API-Key"] == "key"


@respx.mock
def test_order_steam_topup_sends_idempotency_key(fz):
    route = respx.post(f"{BASE}/steam-topup/order").respond(json=ORDER)

    assert fz.order_steam_topup("gaben", "RUB", 500, idempotency_key="54251765")["order"]["id"] == "ord-1"
    assert body(route) == {"steamLogin": "gaben", "currency": "RUB", "amount": 500}
    assert route.calls.last.request.headers["Idempotency-Key"] == "54251765"


@respx.mock
def test_telegram_catalog(fz):
    respx.get(f"{BASE}/telegram/stars").respond(
        json={"ok": True, "price_per_star": "0.0152625", "min_amount": 50, "max_amount": 10000})
    respx.get(f"{BASE}/telegram/premium").respond(
        json={"ok": True, "plans": [{"months": 3, "price_usd": "12.1999"}]})

    assert fz.get_telegram_stars()["min_amount"] == 50
    assert fz.get_telegram_premium()["plans"][0]["months"] == 3


@respx.mock
def test_buy_telegram_stars_and_premium(fz):
    stars = respx.post(f"{BASE}/telegram/stars/buy").respond(json=ORDER)
    premium = respx.post(f"{BASE}/telegram/premium/buy").respond(json=ORDER)

    fz.buy_telegram_stars("@durov", 100, idempotency_key="1")
    fz.buy_telegram_premium("@durov", 12, idempotency_key="2")

    assert body(stars) == {"telegram_username": "@durov", "quantity": 100}
    assert body(premium) == {"telegram_username": "@durov", "months": 12}
    assert stars.calls.last.request.headers["Idempotency-Key"] == "1"


@respx.mock
def test_telegram_buy_not_retried_on_server_error(fz):
    """Покупки Telegram без гарантии идемпотентности — 5xx не повторяем на
    уровне клиента (иначе возможен двойной заказ)."""
    route = respx.post(f"{BASE}/telegram/stars/buy").respond(502, json={"ok": False, "error": "bad_gateway"})

    with pytest.raises(FazerCardsError) as e:
        fz.buy_telegram_stars("@durov", 50)

    assert e.value.status_code == 502 and route.call_count == 1
