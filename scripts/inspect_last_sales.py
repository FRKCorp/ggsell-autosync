"""Проверка: реально ли seller-last-sales содержит id_i (id чата), как
сказала поддержка GGSell — раньше в этом ответе мы видели только
invoice_id/date/product, без id_i.

Запуск: python scripts/inspect_last_sales.py
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellV1Client  # noqa: E402


def main() -> None:
    load_dotenv()
    seller_id = int(os.getenv("GGSELL_V1_SELLER_ID"))
    api_key = os.getenv("GGSELL_V1_API_KEY")
    base_url = os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")

    with GGSellV1Client(seller_id=seller_id, api_key=api_key, base_url=base_url) as client:
        result = client.list_last_sales()
        print("Полный ответ:")
        print(result)
        print()
        sales = result.get("sales", [])
        print(f"Всего продаж в ответе: {len(sales)}")
        for sale in sales:
            print(sale)


if __name__ == "__main__":
    main()
