"""Перевод отдельных лотов App Store в групповые лоты с параметром «Номинал».

Запуск (на сервере — через docker compose exec app):
    python scripts/group_offers.py --dry-run      # что будет сделано, без изменений
    python scripts/group_offers.py                # перевести, лишние лоты — на паузу
    python scripts/group_offers.py --delete       # перевести, лишние лоты — удалить
    python scripts/group_offers.py --only app_store_itunes_tr --dry-run

Какие категории групповые и лимит номинала — data/grouped_categories.json.
В каждой категории лот минимального номинала становится групповым: новое
название, описание, инструкция и цена, параметр «Номинал» с вариантами;
обложка и категория лота остаются. Остальные лоты категории (и номиналы
дороже лимита) — на паузу или удаляются. Групповой лот после смены текстов
может уйти на проверку GGSell.

Повторный запуск безопасен: переведённое не трогается, недоделанное
доделывается. После --pause можно запустить с --delete — удалит лоты на паузе.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellError, GGSellV2Client  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.listing import Listing  # noqa: E402
from app.models.position import Position  # noqa: E402
from app.offers.group_conversion import DELETE, PAUSE, convert_groups  # noqa: E402
from app.offers.groups import group_positions, is_grouped  # noqa: E402
from app.offers.uploader import default_webhook_url  # noqa: E402
from app.pricing.exchange_rate import refresh_rate  # noqa: E402
from app.pricing.listing_price import load_pricing_config  # noqa: E402
from app.sync.showcase import fetch_known_offers  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Перевод лотов App Store в групповые")
    parser.add_argument("--dry-run", action="store_true", help="только показать, что будет сделано")
    parser.add_argument("--delete", action="store_true", help="лишние лоты удалить (по умолчанию — пауза)")
    parser.add_argument("--only", action="append", default=[], help="категория FZ или шаблон с * (можно несколько)")
    parser.add_argument("--pause", type=float, default=0.5, help="пауза между категориями, с")
    args = parser.parse_args()
    mode = DELETE if args.delete else PAUSE

    load_dotenv()
    data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    with open(os.path.join(data_dir, "ggsell_category_map.json"), encoding="utf-8") as f:
        category_map = json.load(f)
    webhook_url = default_webhook_url()

    session = SessionLocal()
    refresh_rate(session)
    session.commit()
    config = load_pricing_config(session)
    positions = [p for p in session.scalars(select(Position).order_by(Position.id)) if is_grouped(p)]
    if args.only:
        positions = [p for p in positions if any(fnmatch.fnmatchcase(p.fz_category_id, o) for o in args.only)]
    groups = group_positions(positions)
    print(f"Курс {config.exchange_rate_usd_to_rub} ₽/$, наценка {config.markup_percent}%. "
          f"Групповых категорий: {len(groups)}, лишние лоты — {'удалить' if mode == DELETE else 'на паузу'}"
          f"{' (dry run)' if args.dry_run else ''}")

    with GGSellV2Client(
        api_key=os.getenv("GGSELL_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
        timeout=30,
    ) as v2:
        print("Загружаю лоты с GGSell…", flush=True)
        try:
            # Все лоты групповых категорий, в том числе archived в БД: лот на паузе
            # или на проверке GGSell ещё существует (на проверке — нет в общем списке).
            offer_ids = session.scalars(select(Listing.ggsell_offer_id).join(Listing.position).where(
                Position.fz_category_id.in_(list(groups))))
            offers = fetch_known_offers(session, v2, offer_ids)
        except GGSellError as e:
            sys.exit(f"GGSell не отдал список лотов ({e.status_code}) — ничего не изменено, попробуйте позже")
        report = convert_groups(
            session, v2, groups, offers, category_map, config, webhook_url=webhook_url, mode=mode,
            dry_run=args.dry_run, pause_seconds=args.pause, on_progress=lambda message: print(message, flush=True),
        )
    session.close()

    action = "удалить" if mode == DELETE else "на паузу"
    if args.dry_run:
        for category, offer_id in report.converted.items():
            n = len(groups[category].members)
            print(f"  🔄 {category}: лот {offer_id} станет групповым (номиналов: {n}), "
                  f"лишних лотов {action}: {len(report.retired.get(category, []))}")
        for category, ids in report.retired.items():
            if category not in report.converted:
                print(f"  🧹 {category}: групповой лот уже есть, лишних лотов {action}: {len(ids)}")
    for category, n in report.over_limit.items():
        print(f"  💲 {category}: номиналов дороже лимита (не продаются): {n}")
    if report.nothing:
        print(f"  ➕ лотов нет — групповой создаст scripts/upload_offers.py: {', '.join(report.nothing)}")
    if report.empty:
        print(f"  ⏭ нет номиналов в продаже у FZ: {', '.join(report.empty)}")

    print(f"\nГотово{' (dry run — на GGSell ничего не менялось)' if args.dry_run else ''}: {report.summary(mode)}")
    for category, error in report.errors.items():
        print(f"  ❌ {category}: {error}")
    sys.exit(1 if report.errors else 0)


if __name__ == "__main__":
    main()
