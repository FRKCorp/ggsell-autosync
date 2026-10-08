"""Проверка: работает ли частичный PATCH для обычного поля (price), в
отличие от delivery (который через PATCH не меняется вообще).

Запуск: python scripts/dev/test_patch_price.py <offer_id>
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.clients.ggsell import GGSellV2Client  # noqa: E402


def show(v2: GGSellV2Client, offer_id: int, label: str) -> None:
    result = v2.get_offer(offer_id)
    offer = result.get("data", result) if isinstance(result, dict) else result
    print(f"[{label}] price={offer.get('price')}")


def main() -> None:
    load_dotenv()
    if len(sys.argv) < 2:
        print("Использование: python scripts/test_patch_price.py <offer_id>")
        sys.exit(1)

    offer_id = int(sys.argv[1])
    api_key = os.getenv("GGSELL_API_KEY")
    base_url = os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")

    with GGSellV2Client(api_key=api_key, base_url=base_url) as v2:
        show(v2, offer_id, "до")

        print("→ PATCH {'price': 2}...")
        v2.patch_offer(offer_id, {"price": 2})
        show(v2, offer_id, "после PATCH price=2")

        print("→ PATCH {'price': 1}... (возврат к исходной)")
        v2.patch_offer(offer_id, {"price": 1})
        show(v2, offer_id, "после PATCH price=1")


if __name__ == "__main__":
    main()
