"""Автозалив черновиков лотов на GGSell.

Запуск:
    python scripts/upload_offers.py --dry-run              # собрать карточки, без GGSell
    python scripts/upload_offers.py --limit 2 --only topup:genshin_impact_global:300
    python scripts/upload_offers.py                         # все позиции из карты категорий
На сервере: docker compose exec app python scripts/upload_offers.py ...

Номиналы групповых категорий (data/grouped_categories.json, App Store)
заливаются одним лотом на категорию с параметром «Номинал».

Повторный запуск безопасен: уже созданные лоты пропускаются, недоделанные
(без опций) — доделываются. Лоты — только черновики (клиент сам ставит
обложки и публикует). Вебхук — GGSELL_WEBHOOK_URL из .env, поэтому боевой
залив делать на том сервере, куда должен приходить вебхук.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys

from dotenv import load_dotenv
from sqlalchemy import or_, select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient  # noqa: E402
from app.clients.ggsell import GGSellV2Client  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.position import Position  # noqa: E402
from app.offers.categories import SPECIAL_CATEGORIES, CategoryTree, resolve_positions  # noqa: E402
from app.offers.group_uploader import upload_groups  # noqa: E402
from app.offers.groups import group_positions, is_grouped  # noqa: E402
from app.offers.uploader import FieldsSource, default_webhook_url, upload_positions  # noqa: E402
from app.pricing.exchange_rate import refresh_rate  # noqa: E402
from app.pricing.listing_price import load_pricing_config  # noqa: E402

SELECTED_CONFIGS = ("selected_topup_categories.json", "selected_giftcard_categories.json")


def load_json(data_dir: str, name: str):
    with open(os.path.join(data_dir, name), encoding="utf-8") as f:
        return json.load(f)


def matches_only(external_id: str, only: list[str]) -> bool:
    """--only: префикс external_id или шаблон с * («giftcard:app_store_itunes_us*»)."""
    return not only or any(
        fnmatch.fnmatchcase(external_id, pattern) if any(c in pattern for c in "*?[") else external_id.startswith(pattern)
        for pattern in only
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Автозалив черновиков на GGSell")
    parser.add_argument("--limit", type=int, help="максимум новых лотов за запуск")
    parser.add_argument("--only", action="append", default=[], help="префикс external_id или шаблон с * (можно несколько)")
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

    # Позиции из карты категорий, Steam/Telegram (категории заданы в коде) и
    # новые позиции выбранных категорий FZ, которых в карте ещё нет.
    known = list(category_map) + list(SPECIAL_CATEGORIES)
    selected = [item["category_id"] for name in SELECTED_CONFIGS for item in load_json(data_dir, name)["items"]]
    query = select(Position).where(
        or_(Position.external_id.in_(known), Position.fz_category_id.in_(selected))
    ).order_by(Position.id)
    positions = [p for p in session.scalars(query) if matches_only(p.external_id, args.only)]
    # Групповую категорию берём целиком, даже если --only выбрал один номинал.
    group_categories = sorted({p.fz_category_id for p in positions if is_grouped(p)})
    singles = [p for p in positions if not is_grouped(p)]
    group_members = list(session.scalars(
        select(Position).where(Position.fz_category_id.in_(group_categories)).order_by(Position.id)
    )) if group_categories else []
    print(f"Позиций к обработке: {len(singles)}, групповых категорий: {len(group_categories)}"
          f"{' (dry run)' if args.dry_run else ''}")

    with GGSellV2Client(
        api_key=os.getenv("GGSELL_API_KEY"),
        base_url=os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com"),
        timeout=30,
    ) as v2, FazerCardsClient(
        api_key=os.getenv("FAZERCARDS_API_KEY"),
        base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2"),
    ) as fz:
        unmapped = [p for p in singles + group_members
                    if p.external_id not in category_map and p.external_id not in SPECIAL_CATEGORIES]
        if unmapped:
            print(f"Новых позиций без категории в карте: {len(unmapped)} — подбираю по правилам")

            def fetch_children(node_id: int) -> list:
                response = v2.list_categories(parent_id=node_id)
                return response.get("data", response) if isinstance(response, dict) else response

            tree = CategoryTree(load_json(data_dir, "ggsell_category_tree.json"), fetch_children=fetch_children)
            resolved, failed = resolve_positions(unmapped, load_json(data_dir, "ggsell_category_rules.json"), tree)
            category_map.update(resolved)
            for external_id, entry in resolved.items():
                mark = "  ⚠️ проверьте категорию" if entry["via_fallback"] or entry["review"] else ""
                print(f"  🗂 {external_id} → {entry['tree']}{mark}")
            by_reason: dict[str, list[str]] = {}
            for external_id, reason in failed.items():
                by_reason.setdefault(reason, []).append(external_id)
            for reason, ids in by_reason.items():
                more = f" и ещё {len(ids) - 3}" if len(ids) > 3 else ""
                print(f"  ⏭ не выставляются ({len(ids)}): {', '.join(ids[:3])}{more} — {reason}")

        report = upload_positions(
            session, v2, singles, category_map, config, FieldsSource(fz, fallback_fields),
            webhook_url=webhook_url, pause_seconds=args.pause, limit=args.limit,
            dry_run=args.dry_run, on_progress=lambda message: print(message, flush=True),
        )
        group_report = upload_groups(
            session, v2, group_positions(group_members), category_map, config,
            webhook_url=webhook_url, pause_seconds=args.pause, dry_run=args.dry_run,
            on_progress=lambda message: print(message, flush=True),
        )
    session.close()

    print(f"\nГотово: {report.summary()}")
    print(f"Групповые лоты: {group_report.summary()}")
    if group_report.pending_conversion:
        print(f"  ⏳ ждут перевода в групповые лоты (scripts/group_offers.py): {', '.join(group_report.pending_conversion)}")
    errors = {**report.errors, **group_report.errors}
    for key, error in errors.items():
        print(f"  ❌ {key}: {error}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
