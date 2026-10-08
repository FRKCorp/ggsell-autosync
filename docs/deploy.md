# Установка на новый сервер

Пошагово: от чистого VPS до работающей системы с залитыми лотами. Ежедневная эксплуатация, бэкапы и мониторинг описаны в [operations.md](operations.md).

**Что нужно:**
- VPS на Ubuntu 22.04 или 24.04, от 2 ГБ памяти и 20 ГБ диска, доступ root по SSH;
- домен или поддомен, A-запись которого указывает на IP сервера;
- аккаунты GGSell (продавец) и FazerCards (активная подписка и баланс);
- Telegram-бот (токен от @BotFather) и chat_id администратора.

Все команды ниже выполняются на сервере от root.

## 1. Docker

```bash
apt-get update && apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
docker compose version
```

## 2. Код

Репозиторий закрытый, поэтому сервер получает к нему доступ по отдельному ключу «только для чтения» (deploy key):

```bash
ssh-keygen -t ed25519 -N "" -C "deploy@$(hostname)" -f ~/.ssh/github_deploy
cat ~/.ssh/github_deploy.pub
```

Выведенную строку добавить в GitHub: репозиторий → Settings → Deploy keys → Add deploy key, без галочки «Allow write access». Затем:

```bash
cat >> ~/.ssh/config <<'EOF'
Host github.com
    IdentityFile ~/.ssh/github_deploy
    IdentitiesOnly yes
EOF
git clone git@github.com:FRKCorp/ggsell-autosync.git /root/ggsell-autosync
cd /root/ggsell-autosync
```

## 3. Файл настроек `.env`

```bash
cp .env.example .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
```

Пароль БД сгенерирован. После первого запуска его не менять, иначе потеряется доступ к созданной базе.

Остальное в `.env` заполняет владелец — строки под пометкой **«ЗАПОЛНИТЕ»** (разделы 1–4 файла: домен, ключи GGSell и FazerCards, бот и chat_id). Редактор: `nano .env`, сохранить — Ctrl+O, Enter, выйти — Ctrl+X.

## 4. База данных

```bash
docker compose build
docker compose up -d db backup
docker compose run --rm app alembic upgrade head
```

## 5. Проверка `.env`

```bash
docker compose run --rm app python scripts/check_env.py
```

Скрипт покажет, что заполнено, и проверит связь с БД, FazerCards (подписка и баланс), GGSell (оба ключа), Telegram (пришлёт тестовое сообщение в бота) и что домен указывает на этот сервер. Ключи он не печатает. Пока везде не ✅ — исправить `.env` и запустить снова.

## 6. Каталог

Импорт позиций из FazerCards по спискам категорий в `data/` (несколько минут):

```bash
docker compose run --rm app python scripts/bulk_import_topups.py
docker compose run --rm app python scripts/bulk_import_giftcards.py
docker compose run --rm app python scripts/import_special.py
```

Карта категорий GGSell (`data/ggsell_category_map.json`) уже в репозитории, пересобирать её не нужно.

## 7. Запуск

```bash
docker compose up -d
docker compose ps
curl -s https://ДОМЕН/health
```

- Caddy сам получит HTTPS-сертификат. Если `curl` не отвечает сразу, подождите минуту и посмотрите `docker compose logs caddy`.
- `scheduler` при старте сразу запустит синхронизацию цен (около 2 минут) и потом будет повторять её каждые 12 часов.
- В Telegram-боте нажать Start, затем «📊 Статус».

## 8. Фаервол

```bash
sh ops/firewall.sh
```

Открытыми останутся только SSH (22), HTTP (80) и HTTPS (443). Если SSH на другом порту: `SSH_PORT=2200 sh ops/firewall.sh`.

## 9. Пробная покупка

Перед заливом всего каталога — один дешёвый лот:

```bash
docker compose exec app python scripts/upload_offers.py --only giftcard:google_play_in:10_inr
```

Опубликовать его в кабинете GGSell, купить и убедиться, что код пришёл в чат заказа, а в боте заказ «✅ выдан». Потом снять лот с публикации.

## 10. Залив каталога

Все позиции, для которых подобрана категория GGSell, заливаются **черновиками** с заглушкой обложки. Добавить обложки и опубликовать — в кабинете GGSell.

```bash
docker compose exec app python scripts/upload_offers.py --dry-run          # проверка без GGSell
docker compose exec app python scripts/upload_offers.py --limit 20         # первые 20
docker compose exec app python scripts/upload_offers.py                    # остальное, около часа
```

- **Повторный запуск безопасен:** уже созданные лоты не дублируются, недосозданные опции досоздаются.
- **Ошибки** печатаются в конце, после исправления причины достаточно запустить залив ещё раз.

## 11. Внешний мониторинг

Сторож внутри системы не сможет сообщить, если сервер упадёт целиком. Поэтому добавьте адрес `https://ДОМЕН/health/full` в бесплатный [UptimeRobot](https://uptimerobot.com) (HTTP-монитор, раз в 5 минут, уведомления на e-mail или в Telegram). Подробно — в [operations.md](operations.md#мониторинг).

## Обновление кода потом

```bash
cd /root/ggsell-autosync
git pull --ff-only
docker compose build app scheduler bot
docker compose run --rm app alembic upgrade head
docker compose up -d
```
