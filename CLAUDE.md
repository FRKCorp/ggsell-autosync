# CLAUDE.md — быстрый старт для Claude Code

Проект: интеграция FazerCards (поставщик) ↔ GGSell (маркетплейс). Синхронизация каталога/цен, автоматическая обработка заказов, автозалив лотов.

**Полная история исследования — в `docs/architecture-notes.md`.** Читай его только когда нужны детали конкретного API-вызова или объяснение, почему что-то сделано именно так — не читай целиком в начале сессии, это лог, не оверью.

## Стек

Python 3.13, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres, APScheduler, httpx, pytest. Деплой: Docker Compose (`db`/`app`/`scheduler`/`caddy`) на VPS, домен `frkcorp.online`, HTTPS через Caddy/Let's Encrypt.

## Что готово и работает на бою (не трогать без причины)

- `app/clients/fazercards.py` — клиент поставщика, rate-limit по категориям, ретраи.
- `app/clients/ggsell.py` — `GGSellV2Client` (каталог, статичный ключ) + `GGSellV1Client` (заказы/чат, логин по подписи).
- `app/models/` — `Position` (кэш каталога FZ), `Listing` (лот на GGSell), `Order` (жизненный цикл заказа).
- `app/sync/` — парсер FZ → `Position`, эффективная группировка запросов по категориям, APScheduler-job каждые 12ч (`app.sync.main`).
- `app/pricing/calculator.py` — курс+наценка+защита от убытка, чистые функции, 11 тестов.
- `app/orders/order_processor.py` — вся логика заказа (webhook → get_order_info → Listing → заказ у FZ → сообщение в чат), 5 тестов. **Последний шаг (create_message) не может завершиться — см. блокер ниже.**
- `app/api/webhooks.py` — реальный вебхук пойман и разобран, подключён к order_processor.
- БД: 528 позиций-топапов, конфиг на ~200 позиций giftcards (`data/selected_giftcard_categories.json`) — **ещё не импортированы в БД**.

## Единственный внешний блокер

**`chat_id` для `create_message`.** Задокументированный `list-of-chats` (`GET /api_sellers/api/debates/v2/chats`) возвращает пустые записи (все поля `null`). Реальный рабочий путь — внутренний `seller.ggsel.com/api/v1/conversations` с браузерной Bearer-сессией — непригоден для сервера. Написано в поддержку GGSell дважды, ждём ответ с примерами запроса/ответа. **Без этого `orders/` не может дойти до `DELIVERED`.**

## Что нужно решить с клиентом перед стартом реализации (не блокер прямо сейчас, но нужно решить рано)

1. Модель каталога: один Offer на SKU, или Offer + variants на номиналы? (клиент попросил позвать его на этом этапе)
2. Overwatch 2 / GearUP Booster PC / Spotify — нет у FazerCards вообще, что делать?
3. Exit Lag — какой из 3 тарифов (Tier 1/2/3)?

## Порядок реализации (после снятия блокеров)

1. `bulk_import_giftcards.py` — по аналогии с `bulk_import_topups.py`, залить ~200 позиций giftcards в БД.
2. Автозалив лотов на GGSell — `create_offer` + `create_or_update_options` для каждой `Position`, с учётом решения по п.1 из списка выше.
3. Реальный `PATCH` цены Position → GGSell offer (сейчас `sync/` обновляет только БД, не витрину). **Частичный PATCH подтверждён рабочим** для обычных полей типа `price` — можно смело использовать.
4. Довести `order_processor.py` до конца (`create_message` с реальным `chat_id`, как только решится блокер).
5. Telegram-бот: алерты + управление + ответ из Telegram пересылается покупателю (тоже упирается в `chat_id`).
6. `/steam-topup` и `/telegram` в `fazercards.py` — динамическая цена, не вписывается в модель `Position`, нужен отдельный механизм.
7. Steam-гифты — отложены клиентом, отдельный будущий заказ, архитектура не спроектирована.

## Важные грабли — не наступать заново

- **GGSell документация систематически расходится с реальностью.** Прежде чем полагаться на задокументированное поведение — проверяй эмпирически на тестовом оффере. Уже подтверждённые расхождения: `delivery` нельзя менять через `PATCH` (только внутренний фронтенд, недоступный нам); `list-of-chats` возвращает пустые записи; пагинация офферов не принимает `per_page`, только `page`.
- **`id_i` означает РАЗНОЕ на разных эндпоинтах GGSell** — номер заказа в вебхуке/`get_order_info`, но id чата в `chats_object`. Не путать.
- Тестовые офферы иногда удаляются поддержкой GGSell без предупреждения — не полагайся на конкретный `offer_id` между сессиями, создавай заново через `scripts/create_test_offer.py`.
- Частичный `PATCH` работает нормально для обычных полей (`price` подтверждено) — проблема была специфична для `delivery`, не для механизма PATCH в целом.
- На сервере переносить локальные изменения (`.env`) вручную — файл не в git, различается между локальной машиной и сервером (`DATABASE_URL` хост `db` vs `localhost`, `GGSELL_WEBHOOK_URL` реальный домен).

## Полезные тестовые скрипты (уже написаны, в `scripts/`)

`create_test_offer.py`, `check_offer.py`, `test_patch_price.py`, `test_patch_delivery.py`, `inspect_order_info.py`, `inspect_chats.py`, `inspect_last_sales.py`, `fetch_all_topup_categories.py`, `fetch_all_giftcard_categories.py`, `bulk_import_topups.py`, `seed_test_positions.py`.
