"""Проверка .env на сервере: что заполнено и что подключается.

Запуск: docker compose run --rm app python scripts/check_env.py

Секреты не печатает — только «заполнено / нет» и результат проверки связи:
БД, FazerCards (подписка, баланс), GGSell API v2 и v1, Telegram-бот (шлёт
тестовое сообщение каждому chat_id) и что домен указывает на этот сервер.
Код выхода 1, если что-то не так.
"""

from __future__ import annotations

import os
import socket
import sys
from typing import Callable

import httpx
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUIRED = {
    "DOMAIN": "домен сервера",
    "GGSELL_API_KEY": "API-ключ GGSell (v2)",
    "GGSELL_V1_SELLER_ID": "ID продавца GGSell",
    "GGSELL_V1_API_KEY": "API-ключ GGSell (v1)",
    "FAZERCARDS_API_KEY": "API-ключ FazerCards",
    "ADMIN_TELEGRAM_BOT_TOKEN": "токен Telegram-бота",
    "ADMIN_TELEGRAM_CHAT_ID": "chat_id админа",
    "POSTGRES_PASSWORD": "пароль БД",
}

failures = 0


def check(title: str, test: Callable[[], str]) -> None:
    global failures
    try:
        print(f"  ✅ {title}: {test()}")
    except Exception as e:  # noqa: BLE001 — показать любую причину
        failures += 1
        print(f"  ❌ {title}: {type(e).__name__}: {str(e)[:200]}")


def check_db() -> str:
    from sqlalchemy import text

    from app.db import SessionLocal

    with SessionLocal() as s:
        s.execute(text("SELECT 1"))
        try:
            positions = s.execute(text("SELECT count(*) FROM positions")).scalar()
        except Exception:  # noqa: BLE001 — таблиц ещё нет
            return "подключение есть, таблиц ещё нет (нужна миграция: alembic upgrade head)"
    return f"подключение есть, позиций каталога: {positions}"


def check_fz() -> str:
    from app.clients.fazercards import FazerCardsClient

    with FazerCardsClient(api_key=os.getenv("FAZERCARDS_API_KEY"),
                          base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")) as fz:
        me = fz.get_me()
        balance = fz.get_balance()
    if not me.get("subscriptionActive"):
        raise RuntimeError(f"подписка не активна (тариф {me.get('plan')}) — продлите в кабинете FazerCards")
    warn = " — ПРОБНЫЙ тариф, продлите до окончания" if me.get("plan") == "trial" else ""
    return (f"аккаунт {me.get('login')}, тариф {me.get('plan')} до {me.get('planExpiresAt')}{warn}, "
            f"баланс ${balance.get('balance')}")


def check_ggsell_v2() -> str:
    from app.clients.ggsell import GGSellV2Client

    with GGSellV2Client(api_key=os.getenv("GGSELL_API_KEY"),
                        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")) as v2:
        offers = v2.list_offers(page=1)
    return f"ключ принят, офферов на первой странице: {len(offers.get('data', []))}"


def check_ggsell_v1() -> str:
    from app.clients.ggsell import GGSellV1Client

    with GGSellV1Client(seller_id=int(os.getenv("GGSELL_V1_SELLER_ID")), api_key=os.getenv("GGSELL_V1_API_KEY"),
                        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")) as v1:
        sales = v1.list_last_sales().get("sales") or []
    return f"вход по подписи прошёл, последних продаж: {len(sales)}"


def check_telegram() -> str:
    from app.clients.telegram import TelegramClient, admin_chat_ids, bot_token

    token, chat_ids = bot_token(), admin_chat_ids()
    if not token:
        raise RuntimeError("токен пустой или не похож на токен бота (должен содержать «:»)")
    if not chat_ids:
        raise RuntimeError("chat_id не задан или не число")
    with TelegramClient(token, timeout=15) as tg:
        name = tg._call("getMe")["username"]
        for chat_id in chat_ids:
            tg.send_message(chat_id, "✅ Проверка связи: бот подключён к серверу.")
    via = "через прокси" if os.getenv("TELEGRAM_PROXY", "").strip() else "напрямую"
    return f"бот @{name} ({via}), тестовое сообщение отправлено: {len(chat_ids)} получателям"


def check_domain() -> str:
    domain = os.getenv("DOMAIN", "").split(",")[0].strip()
    resolved = socket.gethostbyname(domain)
    public_ip = httpx.get("https://api.ipify.org", timeout=10).text.strip()
    if resolved != public_ip:
        raise RuntimeError(f"{domain} указывает на {resolved}, а IP этого сервера {public_ip} — "
                           "исправьте A-запись (обновление DNS может занять до часа)")
    return f"{domain} → {resolved} (этот сервер)"


def main() -> None:
    load_dotenv()
    print("Заполнение .env:")
    missing = [key for key in REQUIRED if not os.getenv(key, "").strip()]
    for key, title in REQUIRED.items():
        print(f"  {'❌ не заполнено' if key in missing else '✅ заполнено'}: {key} — {title}")
    if os.getenv("GGSELL_V1_SELLER_ID", "").strip() and not os.getenv("GGSELL_V1_SELLER_ID").strip().isdigit():
        missing.append("GGSELL_V1_SELLER_ID")
        print("  ❌ GGSELL_V1_SELLER_ID должен быть числом")

    print("\nПроверка связи:")
    check("База данных", check_db)
    if "FAZERCARDS_API_KEY" not in missing:
        check("FazerCards", check_fz)
    if "GGSELL_API_KEY" not in missing:
        check("GGSell API v2", check_ggsell_v2)
    if not {"GGSELL_V1_SELLER_ID", "GGSELL_V1_API_KEY"} & set(missing):
        check("GGSell API v1", check_ggsell_v1)
    if not {"ADMIN_TELEGRAM_BOT_TOKEN", "ADMIN_TELEGRAM_CHAT_ID"} & set(missing):
        check("Telegram-бот", check_telegram)
    if "DOMAIN" not in missing:
        check("Домен", check_domain)

    ok = not missing and not failures
    print("\n" + ("✅ Всё в порядке." if ok else f"❌ Нужно исправить: не заполнено {len(missing)}, ошибок связи {failures}."))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
