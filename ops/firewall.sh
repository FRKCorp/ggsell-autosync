#!/bin/sh
# Фаервол сервера: входящие — только SSH, HTTP и HTTPS.
# Запуск на сервере от root: sh ops/firewall.sh
#   SSH_PORT=2200 sh ops/firewall.sh          — если SSH не на 22
#   EXTRA_PORTS="2096 13019" sh ops/firewall.sh — если на сервере есть другие
#                                               сервисы (VPN, панели): их порты
#   HTTPS_PORT берётся из .env, если запускать из папки проекта.
#
# Безопасно запускать по SSH: правило для SSH добавляется до включения, текущее
# соединение не рвётся. Повторный запуск ничего не ломает.
#
# Важно: Docker публикует порты в обход ufw. Поэтому в docker-compose.yml БД
# слушает только 127.0.0.1:5432, а наружу открыты лишь 80/443 у Caddy.
set -eu

SSH_PORT="${SSH_PORT:-22}"
if [ -z "${HTTPS_PORT:-}" ] && [ -f .env ]; then
    HTTPS_PORT=$(grep -E "^HTTPS_PORT=" .env | cut -d= -f2 | tr -d " ")
fi
HTTPS_PORT="${HTTPS_PORT:-443}"

ufw default deny incoming
ufw default allow outgoing
ufw allow "${SSH_PORT}/tcp" comment "SSH"
ufw allow 80/tcp comment "HTTP - Caddy, Lets Encrypt"
ufw allow "${HTTPS_PORT}/tcp" comment "HTTPS - Caddy, вебхуки GGSell"
for port in ${EXTRA_PORTS:-}; do
    ufw allow "${port}/tcp" comment "другой сервис на сервере"
done
ufw --force enable
ufw status verbose
