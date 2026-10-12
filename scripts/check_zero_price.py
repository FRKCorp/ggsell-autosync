"""Проверка: что будет, если FazerCards вернёт цену 0.

Запуск (на сервере):
    docker compose exec app python scripts/check_zero_price.py --offer 103461016

--offer — номер лота на GGSell (подарочная карта или пополнение по ID).

Берётся настоящий ответ FazerCards по категории лота, и в нём цена этого
лота подменяется на 0 — дальше работает обычный код системы:
  1. Синхронизация цен получает цену 0.
     Ожидаем: цена не сохраняется, цена лота на GGSell не меняется, в бот
     приходит алерт.
  2. Кто-то покупает этот лот, а FZ при проверке перед закупкой отвечает 0.
     Ожидаем: товар у FZ не покупается, заказ уходит на ручной разбор,
     в бот приходит алерт.

Ничего не покупает и ничего не меняет на GGSell (запросы на изменение цены
только записываются и показываются). Алерты в бот приходят с пометкой
«ТЕСТ», чтобы было видно, как они выглядят. База данных не меняется.
"""

from __future__ import annotations

import argparse
import copy
import os
import sys
from typing import Any

from dotenv import load_dotenv
from sqlalchemy import select

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.fazercards import FazerCardsClient  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models.listing import Listing  # noqa: E402
from app.models.position import SourceType  # noqa: E402
from app.orders.notifications import notify_admin  # noqa: E402
from app.orders.order_processor import price_check_failed, verify_price_before_charge  # noqa: E402
from app.pricing.listing_price import listing_price_rub, load_pricing_config  # noqa: E402
from app.sync.fz_catalog import refresh_all_positions  # noqa: E402
from app.sync.showcase import check_availability, push_prices  # noqa: E402

TEST_PREFIX = "🧪 ТЕСТ (проверка «FazerCards вернул цену 0») — так выглядит настоящий алерт:\n\n"


class ZeroPriceFz:
    """Настоящий клиент FZ, но в ответах цена одного товара — 0."""

    def __init__(self, real: FazerCardsClient, category_id: str, offer_id: str):
        self._real, self._category_id, self._offer_id = real, category_id, offer_id

    def _zeroed(self, data: dict[str, Any], category_id: str, id_key: str) -> dict[str, Any]:
        data = copy.deepcopy(data)
        if category_id == self._category_id:
            for offer in data.get("offers", []):
                if offer.get(id_key) == self._offer_id:
                    offer["price_usd"] = "0"
        return data

    def get_topup_offers(self, category_id: str) -> dict[str, Any]:
        return self._zeroed(self._real.get_topup_offers(category_id), category_id, "offer_id")

    def get_giftcard_offers(self, category_id: str) -> dict[str, Any]:
        return self._zeroed(self._real.get_giftcard_offers(category_id), category_id, "card_id")


class RecordingGGSell:
    """Вместо GGSell: изменения цены только записываются."""

    def __init__(self):
        self.patches: list[tuple[int, dict]] = []

    def patch_offer(self, offer_id: int, payload: dict) -> dict:
        self.patches.append((offer_id, payload))
        return {}


def test_alert(message: str) -> None:
    notify_admin(TEST_PREFIX + message)


def main() -> None:
    parser = argparse.ArgumentParser(description="Проверка реакции системы на цену 0 от FazerCards")
    parser.add_argument("--offer", type=int, required=True, help="номер лота на GGSell")
    args = parser.parse_args()

    load_dotenv()
    session = SessionLocal()
    listing = session.scalar(select(Listing).where(Listing.ggsell_offer_id == args.offer))
    if listing is None:
        sys.exit(f"Лот {args.offer} не найден среди лотов системы")
    position = listing.position
    if SourceType(position.source_type) not in (SourceType.TOPUP, SourceType.GIFTCARD):
        sys.exit("Выберите подарочную карту или пополнение по ID (у Steam и Telegram своя схема цен)")

    config = load_pricing_config(session)
    price_before = position.last_known_price_usd
    lot_price_before = listing.price_rub
    print(f"Лот {args.offer}: {position.name}")
    print(f"  цена у FazerCards в базе: ${price_before}, цена лота на GGSell: {lot_price_before} ₽\n")

    with FazerCardsClient(api_key=os.getenv("FAZERCARDS_API_KEY"),
                          base_url=os.getenv("FAZERCARDS_BASE_URL", "https://api.fzr.cards/api/v2")) as real_fz:
        try:  # без нормального ответа FZ проверка ничего не покажет
            real = (real_fz.get_topup_offers if SourceType(position.source_type) == SourceType.TOPUP
                    else real_fz.get_giftcard_offers)(position.fz_category_id)
            id_key = "offer_id" if SourceType(position.source_type) == SourceType.TOPUP else "card_id"
            real_price = next(o["price_usd"] for o in real["offers"] if o[id_key] == position.fz_offer_id)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"FazerCards сейчас не отдаёт этот товар ({e!r}) — проверка невозможна, попробуйте позже "
                     "или выберите другой лот")
        print(f"   (сейчас FazerCards отдаёт цену ${real_price} — в проверке она будет подменена на 0)\n")
        fz = ZeroPriceFz(real_fz, position.fz_category_id, position.fz_offer_id)

        print("1. Синхронизация цен получает от FazerCards цену 0")
        (_, result, error), = refresh_all_positions(session, fz, positions=[position])
        print(f"   ответ системы: {error or 'цена принята (!)'}")
        check_availability(session, [(position, result, error)], alert=test_alert)
        ggsell = RecordingGGSell()
        source_before = listing.price_source_usd_at_sync
        push_prices(session, ggsell, config, pause_seconds=0, alert=test_alert, listings=[listing])
        print(f"   цена в базе: ${position.last_known_price_usd} (была ${price_before})")
        if ggsell.patches:
            # Курс или наценка могли измениться с прошлой синхронизации — тогда цена
            # пересчитывается, но по обычной цене FZ, а не по нулю.
            print(f"   на GGSell ушло бы: {ggsell.patches} — пересчёт по обычной цене FZ (курс/наценка)")
            listing.price_rub, listing.price_source_usd_at_sync = lot_price_before, source_before
            session.commit()  # запись — только в базе, GGSell не трогали: возвращаем как было
        else:
            print("   на GGSell ничего не отправлено — цена лота не меняется")
        sync_ok = (result is None and position.last_known_price_usd == price_before
                   and all(payload["price"] >= float(lot_price_before) * 0.5 for _, payload in ggsell.patches))
        print(f"   {'✅ цена 0 не прошла' if sync_ok else '❌ ЦЕНА 0 ПРОШЛА'}\n")

        print("2. Покупка лота: FazerCards при проверке перед закупкой отвечает 0")
        try:
            verify_price_before_charge(fz, position, config)
            print("   ❌ проверка пропустила цену 0 — заказ был бы куплен")
            order_ok = False
        except Exception as e:  # noqa: BLE001 — так же, как в process_new_order
            _, alert = price_check_failed("ТЕСТ", e, {"пример": "данные покупателя"})
            test_alert(alert)
            print(f"   ответ системы: {e}")
            print("   ✅ товар у FazerCards не покупается, заказ уходит на ручной разбор\n")
            order_ok = True

    session.rollback()
    print(f"Расчётная цена лота сейчас: {listing_price_rub(listing, config)} ₽ — по обычной цене FazerCards.")
    session.close()
    print("Итог: " + ("✅ система не продаёт и не покупает по цене 0. В бот пришли тестовые алерты."
                      if sync_ok and order_ok else "❌ есть проблема — пришлите этот вывод разработчикам"))
    sys.exit(0 if sync_ok and order_ok else 1)


if __name__ == "__main__":
    main()
