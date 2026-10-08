#!/bin/sh
# Ежедневный дамп БД — сервис `backup` в docker-compose.yml.
#
# Раз в сутки в BACKUP_AT_UTC (по умолчанию 00:30 UTC = 03:30 МСК) делает
# pg_dump в /backups (= ./backups на сервере), хранит BACKUP_KEEP последних
# (по умолчанию 7). При старте сразу делает дамп, если свежего (< 24 ч) нет —
# чтобы после деплоя бэкап был сразу. Свежесть бэкапа проверяет сторож в
# scheduler (app/ops/watchdog.py) — если дамп перестал появляться, придёт алерт.
#
# Восстановление — INSTRUCTIONS.md.
set -eu

KEEP="${BACKUP_KEEP:-7}"
AT="${BACKUP_AT_UTC:-00:30}"
DIR=/backups

dump() {
    name="ggsell_$(date -u +%Y%m%d_%H%M%S).dump"
    if pg_dump -Fc -f "$DIR/.tmp_$name"; then
        mv "$DIR/.tmp_$name" "$DIR/$name"
        echo "$(date -u '+%F %T') backup ok: $name ($(du -h "$DIR/$name" | cut -f1))"
        # Старые — удалить, оставить KEEP последних
        ls -1t "$DIR"/ggsell_*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm -f
    else
        rm -f "$DIR/.tmp_$name"
        echo "$(date -u '+%F %T') backup FAILED" >&2
    fi
}

seconds_until() {  # до ближайшего HH:MM UTC
    target_h=$(echo "$1" | cut -d: -f1 | sed 's/^0//'); target_m=$(echo "$1" | cut -d: -f2 | sed 's/^0//')
    now_h=$(date -u +%H | sed 's/^0//'); now_m=$(date -u +%M | sed 's/^0//'); now_s=$(date -u +%S | sed 's/^0//')
    diff=$(( (${target_h:-0} * 3600 + ${target_m:-0} * 60) - (${now_h:-0} * 3600 + ${now_m:-0} * 60 + ${now_s:-0}) ))
    [ "$diff" -le 0 ] && diff=$((diff + 86400))
    echo "$diff"
}

mkdir -p "$DIR"
until pg_isready -q; do sleep 2; done

if [ -z "$(find "$DIR" -name 'ggsell_*.dump' -mmin -1440 2>/dev/null)" ]; then
    dump
fi
while true; do
    sleep "$(seconds_until "$AT")"
    dump
done
