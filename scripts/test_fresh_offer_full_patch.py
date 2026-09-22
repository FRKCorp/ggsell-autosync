"""Проверка гипотезы: свежесозданный оффер требует ПОЛНЫЙ PATCH (весь
объект, как при create), а не частичный {"delivery": "manual"}, чтобы
поле delivery реально применилось.

Создаёт ещё один новый тестовый оффер и пробует оба варианта по очереди.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellV2Client  # noqa: E402

TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def show(v2: GGSellV2Client, offer_id: int, label: str) -> dict:
    result = v2.get_offer(offer_id)
    offer = result.get("data", result) if isinstance(result, dict) else result
    print(f"[{label}] status={offer.get('status')} delivery={offer.get('delivery')}")
    return offer


def main() -> None:
    load_dotenv()
    api_key = os.getenv("GGSELL_API_KEY")
    base_url = os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")

    # Та же категория-лист, что и раньше (Игры > LINEAGE II > Адена, id=14415) —
    # чтобы сравнение было чистым, без влияния другой категории.
    category_id = 14415

    base_payload = {
        "title_ru": "fz Тест PATCH — не публиковать",
        "title_en": "fz Test PATCH — do not publish",
        "description_ru": "Тест: нужен ли полный PATCH. Безопасно удалить.",
        "description_en": "Test: full patch hypothesis. Safe to delete.",
        "instructions_ru": "Тест.",
        "instructions_en": "Test.",
        "cover_image_ru": f"data:image/png;base64,{TINY_PNG_BASE64}",
        "price": 1,
        "currency": "RUB",
        "is_autoselling": False,
        "category_id": category_id,
        "min_quantity": 1,
        "max_quantity": 1,
        "is_unlimited_quantity": True,
        "delivery": "manual",
    }

    with GGSellV2Client(api_key=api_key, base_url=base_url) as v2:
        print("→ Создаю новый тестовый оффер (delivery=manual сразу в create)...")
        result = v2.create_offer(base_payload)
        offer = result.get("data", result) if isinstance(result, dict) else result
        offer_id = offer["id"]
        print(f"  id={offer_id}")
        show(v2, offer_id, "сразу после create")

        print("\n→ Пробую ЧАСТИЧНЫЙ PATCH {'delivery': 'manual'}...")
        v2.patch_offer(offer_id, {"delivery": "manual"})
        after_partial = show(v2, offer_id, "после частичного PATCH")

        if after_partial.get("delivery") != "manual":
            print("\n→ Частичный не сработал. Пробую ПОЛНЫЙ PATCH (весь объект)...")
            full_payload = {**base_payload, "delivery": "manual"}
            v2.patch_offer(offer_id, full_payload)
            show(v2, offer_id, "после полного PATCH")
        else:
            print("\n✅ Частичный PATCH сработал на этот раз — предыдущая неудача была разовой аномалией.")

        print(f"\nID этого тестового оффера для последующей очистки: {offer_id}")


if __name__ == "__main__":
    main()
