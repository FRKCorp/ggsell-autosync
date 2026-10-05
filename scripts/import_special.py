"""Импорт позиций этапа 7 — пополнение Steam (RUB/KZT/UAH/USD), Telegram
Stars и Premium (app/sync/fz_special.py). Повторный запуск обновляет цены.

Запуск: docker compose exec app python scripts/import_special.py [steam|stars|premium ...]
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.position import SourceType  # noqa: E402
from app.sync.fz_special import import_special_positions  # noqa: E402

KINDS = {
    "steam": SourceType.STEAM_TOPUP,
    "stars": SourceType.TELEGRAM_STARS,
    "premium": SourceType.TELEGRAM_PREMIUM,
}


def main() -> None:
    load_dotenv()
    kinds = [KINDS[k] for k in sys.argv[1:]] or None
    session = SessionLocal()
    try:
        with FazerCardsClient(
            api_key=os.getenv("FAZERCARDS_API_KEY"),
            base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2"),
        ) as fz:
            results = import_special_positions(session, fz, kinds)
        for r in results:
            p = r.position
            raw = p.raw_payload or {}
            units = f", {raw['min_units']}–{raw['max_units']} {raw.get('unit', '')}" if "min_units" in raw else ""
            status = "создана" if r.created else ("цена изменилась" if r.price_changed else "без изменений")
            print(f"{p.external_id}: {p.name} — ${p.last_known_price_usd}{units} ({status})")
    finally:
        session.close()


if __name__ == "__main__":
    main()
