"""Эмпирическая проверка опций на живом GGSell (roadmap 3.2): вешает на
оффер опции покупателя для категории топапа (по data/fz_topup_fields.json)
и печатает, что реально получилось на стороне GGSell.

Запуск:
    python scripts/create_test_offer.py            # создать черновик, взять id
    python scripts/test_attach_options.py <offer_id> [fz_category_id]
    python scripts/test_attach_options.py <offer_id> --delete   # удалить оффер

fz_category_id по умолчанию — wuthering_waves (есть и text, и select).
"""

from __future__ import annotations

import json
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellError, GGSellV2Client  # noqa: E402
from app.offers.options import attach_topup_options  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        print("Использование: python scripts/test_attach_options.py <offer_id> [fz_category_id | --delete]")
        sys.exit(1)
    load_dotenv()
    offer_id = int(sys.argv[1])
    arg = sys.argv[2] if len(sys.argv) > 2 else "wuthering_waves"

    with GGSellV2Client(
        api_key=os.getenv("GGSELL_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
    ) as v2:
        if arg == "--delete":
            print(v2.batch_delete_offers([offer_id]))
            return

        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        with open(os.path.join(data_dir, "fz_topup_fields.json"), encoding="utf-8") as f:
            fields = json.load(f)[arg]["fields"]

        print(f"→ Вешаю опции {arg} на оффер {offer_id}: {[f_['key'] for f_ in fields]}")
        try:
            options = attach_topup_options(v2, offer_id, fields)
        except GGSellError as e:
            print(f"❌ GGSell: status={e.status_code} payload={e.payload}")
            sys.exit(1)

        print("✅ Опции на оффере после создания:")
        print(json.dumps(options, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
