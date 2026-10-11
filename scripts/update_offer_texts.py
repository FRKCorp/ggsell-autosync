"""Замена фразы в описаниях и инструкциях уже созданных лотов — без перезалива.

Запуск (на сервере — через docker compose exec app):
    python scripts/update_offer_texts.py --pair "до 01:00" "до 22:00" --pair "to 01:00" "to 22:00" --dry-run
    python scripts/update_offer_texts.py --pair "до 01:00" "до 22:00" --pair "to 01:00" "to 22:00"

--pair СТАРОЕ НОВОЕ — что на что заменить (можно несколько). Сначала
запустите с --dry-run: покажет, сколько лотов изменится. Меняется только
найденная фраза, остальной текст (в том числе ваши ручные правки) остаётся.
Затрагиваются только лоты, созданные системой. Повторный запуск безопасен.

Шаблоны новых лотов — в data/offer_templates.json, их правят отдельно.
"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellError, GGSellV2Client  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.offers.text_update import update_offer_texts  # noqa: E402
from app.sync.showcase import fetch_offers  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Замена фразы в текстах лотов на GGSell")
    parser.add_argument("--pair", nargs=2, action="append", required=True, metavar=("СТАРОЕ", "НОВОЕ"),
                        help="что на что заменить (можно несколько)")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать, без изменений на GGSell")
    parser.add_argument("--pause", type=float, default=0.2, help="пауза между лотами, с")
    args = parser.parse_args()
    pairs = [(old, new) for old, new in args.pair if old]
    if not pairs:
        sys.exit("Пустая фраза для поиска")

    load_dotenv()
    session = SessionLocal()
    with GGSellV2Client(
        api_key=os.getenv("GGSELL_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
        timeout=30,
    ) as v2:
        print("Загружаю лоты с GGSell…", flush=True)
        try:
            offers = fetch_offers(v2)
        except GGSellError as e:
            sys.exit(f"GGSell не отдал список лотов ({e.status_code}) — ничего не изменено, попробуйте позже")
        for old, new in pairs:
            print(f"  «{old}» → «{new}»")
        report = update_offer_texts(session, v2, offers, pairs, dry_run=args.dry_run, pause_seconds=args.pause,
                                    on_progress=lambda message: print(message, flush=True))
    session.close()

    print(f"\nГотово{' (dry run — на GGSell ничего не менялось)' if args.dry_run else ''}: {report.summary()}")
    for offer_id, error in report.errors.items():
        print(f"  ❌ {offer_id}: {error}")
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
