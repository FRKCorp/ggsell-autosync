"""Разведка: смотрим реальную структуру get_order_info для конкретного
заказа — нужно понять, чему соответствует id_d из вебхука (offer_id?
listing id?), и увидеть реальные options[].user_data.

Запуск: python scripts/inspect_order_info.py <invoice_id>
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellV1Client  # noqa: E402


def main() -> None:
    load_dotenv()
    if len(sys.argv) < 2:
        print("Использование: python scripts/inspect_order_info.py <invoice_id>")
        sys.exit(1)

    invoice_id = int(sys.argv[1])
    seller_id = int(os.getenv("GGSELL_V1_SELLER_ID"))
    api_key = os.getenv("GGSELL_V1_API_KEY")
    base_url = os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")

    with GGSellV1Client(seller_id=seller_id, api_key=api_key, base_url=base_url) as client:
        result = client.get_order_info(invoice_id)
        print(result)


if __name__ == "__main__":
    main()
