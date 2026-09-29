# CLAUDE.md — быстрый старт для Claude Code

Проект: интеграция FazerCards (поставщик) ↔ GGSell (маркетплейс). Синхронизация каталога/цен, автоматическая обработка заказов, автозалив лотов.

**План работ и прогресс — `docs/roadmap.md`.** Это чек-лист от текущего состояния до сдачи клиенту: работаем строго по нему, пункт закрываем (`[x]` + дата) только когда выполнен его критерий готовности. Читай в начале сессии, чтобы понять, где остановились.

**История исследования API — `docs/architecture-notes.md`.** Читай только когда нужны детали конкретного API-вызова или объяснение, почему что-то сделано именно так — не читай целиком в начале сессии, это лог, не оверью.

## Стек

Python 3.13, FastAPI, SQLAlchemy 2.0 + Alembic, Postgres, APScheduler, httpx, pytest. Деплой: Docker Compose (`db`/`app`/`scheduler`/`caddy`) на VPS, домен `frkcorp.online`, HTTPS через Caddy/Let's Encrypt.

Локально: `venv/Scripts/python -m pytest` (в системном Python pytest нет).

## Прод-сервер и деплой

- Доступ: `ssh frkcorp` (алиас в `~/.ssh/config`, root@89.110.92.55). Проект в `/root/ggsell-autosync`.
- Деплой: `ssh frkcorp 'cd /root/ggsell-autosync && git pull --ff-only && docker compose up -d --build app scheduler'`, затем проверка `curl -s https://frkcorp.online/health`.
- Миграции: `docker compose exec app alembic upgrade head`. Скрипты: `docker compose exec app python scripts/<script>.py`.
- БД: `docker compose exec -T db sh -c 'psql -U $POSTGRES_USER -d $POSTGRES_DB'`.
- Логи: `docker compose logs --since 1h scheduler` / `app`.
- Перед изменением прода (пересборка, импорт, миграции, `.env`) — коротко сказать пользователю, что делаешь. Чтение логов/статуса/SELECT — свободно.

## Что готово и работает на бою (не трогать без причины)

- `app/clients/fazercards.py` — клиент поставщика, rate-limit по категориям, ретраи.
- `app/clients/ggsell.py` — `GGSellV2Client` (каталог, статичный ключ) + `GGSellV1Client` (заказы/чат, логин по подписи).
- `app/models/` — `Position` (кэш каталога FZ), `Listing` (лот на GGSell), `Order` (жизненный цикл заказа).
- `app/sync/` — парсер FZ → `Position`, группировка запросов по категориям, APScheduler-job каждые 12ч (`app.sync.main`).
- `app/pricing/calculator.py` — курс+наценка+защита от убытка, чистые функции, 11 тестов.
- `app/orders/order_processor.py` — вся логика заказа end-to-end, включая `create_message(invoice_id)` покупателю, 5 тестов.
- `app/api/webhooks.py` — реальный вебхук пойман и разобран, подключён к order_processor.
- Прод-БД: 526 позиций топапов (импорт 29 сентября). Giftcards (236 категорий в `data/selected_giftcard_categories.json`) — ещё не импортированы, roadmap этап 1.

## Что ждём от клиента

Решения перед автозаливом лотов (модель каталога, шаблоны карточек, обложки, категории, наценка/курс) — собраны в roadmap, этап 2. Клиент просил позвать его отдельно на этом этапе.

## Важные грабли — не наступать заново

- **GGSell документация систематически расходится с реальностью.** Прежде чем полагаться на задокументированное поведение — проверяй эмпирически на тестовом оффере. Подтверждённые расхождения: `delivery` нельзя менять через `PATCH` (только внутренний фронтенд, недоступный нам); пагинация офферов не принимает `per_page`, только `page`.
- **Все офферы создаём с `delivery: "auto"`** — сменить потом нельзя (подтверждено поддержкой GGSell, notes 3.8). Выдача товара всё равно через `create_message`.
- **`id_i` для `create_message` — это просто `invoice_id`**, отдельной сущности чата нет. `list_chats` (возвращает `id_i: null`) — признанный поддержкой баг, не используй его.
- Частичный `PATCH` работает нормально для обычных полей (`price` подтверждено) — проблема была специфична для `delivery`.
- Тестовые офферы иногда удаляются поддержкой GGSell без предупреждения — не полагайся на конкретный `offer_id` между сессиями, создавай заново через `scripts/create_test_offer.py`.
- **Локальная БД ≠ прод-БД.** Импорт, прогнанный локально, на сервере не появляется — скрипты импорта надо запускать и на сервере (так прод-БД до 29 сентября оставалась пустой).
- На сервере `.env` заполняется вручную — файл не в git, различается между локальной машиной и сервером (`DATABASE_URL` хост `db` vs `localhost`, `GGSELL_WEBHOOK_URL` реальный домен).
- FazerCards подписка может протухнуть — если все вызовы вдруг начали падать с `403 subscription_inactive`, это не баг кода, проверь статус подписки на аккаунте.
- SSH с не-домашних сетей может не работать: там бывает заблокирован исходящий порт 22 (проблема сети, не сервера).

## Полезные тестовые скрипты (уже написаны, в `scripts/`)

`create_test_offer.py`, `check_offer.py`, `test_patch_price.py`, `test_patch_delivery.py`, `inspect_order_info.py`, `inspect_chats.py`, `inspect_last_sales.py`, `fetch_all_topup_categories.py`, `fetch_all_giftcard_categories.py`, `bulk_import_topups.py`, `seed_test_positions.py`.
