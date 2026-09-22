"""
Создаёт один тестовый оффер на GGSell (через V2 API) — минимальный, чтобы
проверить: 1) появляется ли карточка вообще, 2) в каком статусе она
создаётся по умолчанию, 3) прилетает ли что-то на GGSELL_WEBHOOK_URL.

Запуск из корня проекта:
    python scripts/create_test_offer.py

ВНИМАНИЕ: создаёт реальный оффер в твоём личном кабинете GGSell (пусть и
черновик). После проверки его можно будет удалить вручную из кабинета или
через v2.batch_delete_offers([offer_id]).
"""

from __future__ import annotations

import base64
import os
import sys

from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.clients.ggsell import GGSellError, GGSellV2Client  # noqa: E402

# 1x1 прозрачный PNG — просто чтобы проверить, обязательно ли поле вообще
# принимает картинку и не падает на валидации формата.
TINY_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def main() -> None:
    load_dotenv()
    api_key = os.getenv("GGSELL_API_KEY")
    base_url = os.getenv("GGSELL_BASE_URL", "https://seller.ggsel.com")
    webhook_url = os.getenv("GGSELL_WEBHOOK_URL")

    with GGSellV2Client(api_key=api_key, base_url=base_url) as v2:
        print("→ Смотрю сырую структуру списка категорий...")
        categories = v2.list_categories()
        items = categories.get("data", categories) if isinstance(categories, dict) else categories
        print(f"  Всего категорий в ответе: {len(items)}")
        print("  Первые 10 (сырые объекты):")
        for c in items[:10]:
            print(f"    {c}")

        # Все категории верхнего уровня имеют has_children=True. Рабочий параметр
        # подтверждён эмпирически — это parent_id. Спускаемся рекурсивно, пока
        # не найдём реальный лист (has_children=False), с защитой от бесконечного цикла.
        current = next((c for c in items if "Игры" in c.get("title", "")), items[0])
        depth = 0
        while current.get("has_children") and depth < 8:
            depth += 1
            resp = v2.list_categories(parent_id=current["id"])
            level = resp.get("data", resp) if isinstance(resp, dict) else resp
            if not level:
                print(f"\n❌ parent_id={current['id']} вернул пустой список, хотя has_children=True. Останавливаюсь.")
                sys.exit(1)
            print(f"  Уровень {depth}: '{current['title']}' -> {len(level)} подкатегорий, первая: {level[0]}")
            current = level[0]

        category = current
        print(f"\n  Выбрал категорию (глубина {depth}): id={category['id']}, title={category.get('title')}, tree={category.get('tree')}, has_children={category.get('has_children')}")

        payload = {
            "title_ru": "fz Тестовый лот — не публиковать",
            "title_en": "fz Test offer — do not publish",
            "description_ru": "Тестовый оффер для проверки интеграции. Безопасно удалить.",
            "description_en": "Test offer for integration check. Safe to delete.",
            "instructions_ru": "Тестовая инструкция.",
            "instructions_en": "Test instructions.",
            "cover_image_ru": f"data:image/png;base64,{TINY_PNG_BASE64}",
            "price": 1,
            "currency": "RUB",
            "is_autoselling": False,
            "category_id": category["id"],
            "min_quantity": 1,
            "max_quantity": 1,
            "is_unlimited_quantity": True,
            "delivery": "manual",
        }
        if webhook_url:
            payload["notification_settings"] = {
                "type": "url",
                "url": webhook_url,
                "http_method": "POST",
                "is_disabled": False,
                "is_default": False,
            }

        print("→ Создаю тестовый оффер...")
        try:
            result = v2.create_offer(payload)
        except GGSellError as e:
            print(f"❌ Ошибка создания оффера: status={e.status_code}")
            print(f"   payload ответа: {e.payload}")
            sys.exit(1)

        print("✅ Оффер создан. Полный ответ API:")
        print(result)

        offer = result.get("data", result) if isinstance(result, dict) else result
        offer_id = offer.get("id")
        status = offer.get("status")
        delivery = offer.get("delivery")
        print()
        print(f"id оффера: {offer_id}")
        print(f"статус: {status}  {'✅ (черновик, как надо)' if status == 'draft' else '⚠️ — проверь, ожидали draft'}")
        print(f"delivery: {delivery}  {'✅' if delivery == 'manual' else '⚠️ отправляли manual, пришло другое — пробую PATCHом'}")

        if delivery != "manual" and offer_id:
            print()
            print(f"→ Пробую PATCH /offers/{offer_id} с только {{'delivery': 'manual'}} (частичный patch)...")
            try:
                patch_result = v2.patch_offer(offer_id, {"delivery": "manual"})
                print("  Ответ:")
                print(f"  {patch_result}")
                patched_offer = patch_result.get("data", patch_result) if isinstance(patch_result, dict) else patch_result
                new_delivery = patched_offer.get("delivery") if isinstance(patched_offer, dict) else None
                if new_delivery == "manual":
                    print("  ✅ Частичный PATCH сработал — достаточно отправлять только изменённое поле, не весь объект.")
                else:
                    print(f"  ⚠️ delivery после PATCH всё ещё '{new_delivery}' — частичный PATCH не сработал как ожидалось.")
            except GGSellError as e:
                print(f"  ❌ PATCH вернул ошибку: status={e.status_code} payload={e.payload}")

        print()
        print("Дальше: загляни в личный кабинет GGSell — появилась ли карточка в списке")
        print("товаров/черновиков. Если настроен webhook — проверь webhook.site на предмет")
        print("входящих запросов (хотя на этапе просто создания оффера их, скорее всего,")
        print("не будет — webhook сработает только при реальной продаже/заказе).")


if __name__ == "__main__":
    main()
