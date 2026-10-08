#!/bin/sh
# Проверка, что последний бэкап восстанавливается: разворачивает
# дамп во временную базу restore_check, сравнивает число строк в таблицах с
# рабочей базой и удаляет временную. Рабочую базу НЕ трогает.
# Запуск: docker compose exec -T backup sh /ops/restore_check.sh
set -eu
latest=$(ls -1t /backups/ggsell_*.dump | head -1)
echo "Проверяю $latest"
psql -q -d postgres -c "DROP DATABASE IF EXISTS restore_check" -c "CREATE DATABASE restore_check"
pg_restore --no-owner -d restore_check "$latest"
status=0
# Бизнес-данные: после бэкапа меняются редко — должны совпасть (если между
# бэкапом и проверкой были новые заказы/позиции — расхождение ожидаемо).
for table in positions listings orders; do
    live=$(psql -At -c "SELECT count(*) FROM $table")
    restored=$(psql -At -d restore_check -c "SELECT count(*) FROM $table")
    mark=ok; [ "$live" = "$restored" ] || { mark="РАСХОЖДЕНИЕ"; status=1; }
    echo "$table: рабочая $live, из бэкапа $restored — $mark"
done
# settings пишется постоянно (сердцебиения сервисов) — только для справки.
echo "settings: рабочая $(psql -At -c 'SELECT count(*) FROM settings'), из бэкапа $(psql -At -d restore_check -c 'SELECT count(*) FROM settings') (меняется постоянно — не сверяется)"
psql -q -d postgres -c "DROP DATABASE restore_check"
exit $status
