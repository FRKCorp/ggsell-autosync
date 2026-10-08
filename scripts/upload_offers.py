"""Автозалив черновиков лотов на GGSell.

Запуск:
    python scripts/upload_offers.py --dry-run              # собрать карточки, без GGSell
    python scripts/upload_offers.py --limit 2 --only topup:genshin_impact_global:300
    python scripts/upload_offers.py                         # все позиции из карты категорий
На сервере: docker compose exec app python scripts/upload_offers.py ...

Повторный запуск безопасен: уже созданные лоты пропускаются, недоделанные
(без опций) — доделываются. Лоты — только черновики (клиент сам ставит
обложки и публикует). Вебхук — GGSELL_WEBHOOK_URL из .env, поэтому боевой
залив делать на том сервере, куда должен приходить вебхук.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient  # noqa: E402
from app.clients.ggsell import GGSellV2Client  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.position import Position  # noqa: E402
from app.offers.categories import SPECIAL_CATEGORIES  # noqa: E402
from app.offers.uploader import FieldsSource, default_webhook_url, upload_positions  # noqa: E402
from app.pricing.exchange_rate import refresh_rate  # noqa: E402
from app.pricing.listing_price import load_pricing_config  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Автозалив черновиков на GGSell")
    parser.add_argument("--limit", type=int, help="максимум новых лотов за запуск")
    parser.add_argument("--only", action="append", default=[], help="префикс external_id (можно несколько)")
    parser.add_argument("--dry-run", action="store_true", help="только собрать карточки, без GGSell")
    parser.add_argument("--pause", type=float, default=0.5, help="пауза между созданием лотов, с")
    args = parser.parse_args()

    load_dotenv()
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    with open(os.path.join(data_dir, "ggsell_category_map.json"), encoding="utf-8") as f:
        category_map = json.load(f)
    with open(os.path.join(data_dir, "fz_topup_fields.json"), encoding="utf-8") as f:
        fallback_fields = {k: v["fields"] for k, v in json.load(f).items()}

    webhook_url = default_webhook_url()
    if not webhook_url and not args.dry_run:
        sys.exit("Не задан DOMAIN (или GGSELL_WEBHOOK_URL) в .env — без адреса вебхука GGSell не сообщит о продажах")

    session = SessionLocal()
    refresh_rate(session)
    session.commit()
    config = load_pricing_config(session)
    print(f"Курс {config.exchange_rate_usd_to_rub} ₽/$, глобальная наценка {config.markup_percent}%, вебхук {webhook_url}")

    known = list(category_map) + list(SPECIAL_CATEGORIES)  # Steam/Telegram — категории заданы в коде
    query = select(Position).where(Position.external_id.in_(known)).order_by(Position.id)
    positions = [
        p for p in session.scalars(query)
        if not args.only or any(p.external_id.startswith(prefix) for prefix in args.only)
    ]
    print(f"Позиций к обработке: {len(positions)}{' (dry run)' if args.dry_run else ''}")

    with GGSellV2Client(
        api_key=os.getenv("GGSELL_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
        timeout=30,
    ) as v2, FazerCardsClient(
        api_key=os.getenv("FAZERCARDS_API_KEY"),
        base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2"),
    ) as fz:
        report = upload_positions(
            session, v2, positions, category_map, config, FieldsSource(fz, fallback_fields),
            webhook_url=webhook_url, pause_seconds=args.pause, limit=args.limit,
            dry_run=args.dry_run, on_progress=lambda message: print(message, flush=True),
        )
    session.close()

    print(f"\nГотово: {report.summary()}")
    for external_id, error in report.errors.items():
        print(f"  ❌ {external_id}: {error}")
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
