"""Тестовый импорт нескольких позиций вручную (по одной каждого типа) —
проверка, что цепочка FazerCards -> БД реально работает, пока нет полного
списка ~2000 позиций от клиента.

Запуск: python scripts/seed_test_positions.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.sync.fz_catalog import import_giftcard, import_steam_gift, import_topup  # noqa: E402


def main() -> None:
    load_dotenv()
    api_key = os.getenv("FAZERCARDS_API_KEY")
    base_url = os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")

    session = SessionLocal()
    try:
        with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
            print("→ import_topup('8_ball_pool', 'golden_spin')")
            r1 = import_topup(session, client, "8_ball_pool", "golden_spin")
            print(f"  {r1}")

            print("\n→ import_giftcard('acash_my', '10_myr')")
            r2 = import_giftcard(session, client, "acash_my", "10_myr")
            print(f"  {r2}")

            print("\n→ import_steam_gift(appid=730, sub_id=54029, region='RU')")
            r3 = import_steam_gift(session, client, 730, 54029, region="RU")
            print(f"  {r3}")

        session.commit()
        print("\n✅ Закоммичено в БД.")

        print("\n→ Повторный запуск (те же позиции) — должно быть created=False:")
        with FazerCardsClient(api_key=api_key, base_url=base_url) as client:
            r1b = import_topup(session, client, "8_ball_pool", "golden_spin")
            print(f"  {r1b}")
        session.commit()

    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
