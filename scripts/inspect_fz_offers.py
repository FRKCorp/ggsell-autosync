"""Разведка: как выглядят реальные офферы/цены у FazerCards по каждому типу
источника. Нужно один раз посмотреть сырую структуру, прежде чем писать
модуль записи в БД (чтобы не гадать с именами полей цены).

Запуск: python scripts/inspect_fz_offers.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient  # noqa: E402


def main() -> None:
    load_dotenv()
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
        print("=" * 60)
        print("TOPUPS")
        cats = client.list_topup_categories(limit=3)
        print("Категории (сырые):")
        print(cats)
        first_cat = cats.get("items", [{}])[0]
        cat_id = first_cat.get("category_id")
        if cat_id:
            print(f"\n→ get_topup_offers(category_id={cat_id!r})")
            offers = client.get_topup_offers(cat_id)
            print(offers)

        print("\n" + "=" * 60)
        print("GIFTCARDS")
        gcats = client.list_giftcard_categories(limit=3)
        print("Категории (сырые):")
        print(gcats)
        first_gcat = gcats.get("items", [{}])[0]
        gcat_id = first_gcat.get("category_id")
        if gcat_id:
            print(f"\n→ get_giftcard_offers(category_id={gcat_id!r})")
            gcards = client.get_giftcard_offers(gcat_id)
            print(gcards)

        print("\n" + "=" * 60)
        print("STEAM GIFTS")
        games = client.list_steam_gift_games(limit=3)
        print("Игры (сырые):")
        print(games)
        first_game = games.get("games", [{}])[0]
        appid = first_game.get("appid")
        if appid:
            print(f"\n→ get_steam_gift_offers(appid={appid!r})")
            offer = client.get_steam_gift_offers(appid)
            print(offer)


if __name__ == "__main__":
    main()
