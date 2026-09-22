"""Быстрая проверка текущего состояния тестового оффера через API.

Запуск: python scripts/check_offer.py <offer_id>
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellV2Client  # noqa: E402


def main() -> None:
    load_dotenv()
    if len(sys.argv) < 2:
        print("Использование: python scripts/check_offer.py <offer_id>")
        sys.exit(1)

    offer_id = int(sys.argv[1])
    api_key = os.getenv("GGSELL_API_KEY")
    base_url = os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")

    with GGSellV2Client(api_key=api_key, base_url=base_url) as v2:
        result = v2.get_offer(offer_id)
        offer = result.get("data", result) if isinstance(result, dict) else result
        print(f"status:   {offer.get('status')}")
        print(f"delivery: {offer.get('delivery')}")
        print()
        print("Полный ответ:")
        print(offer)


if __name__ == "__main__":
    main()
