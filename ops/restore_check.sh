#!/bin/sh
# Проверка, что последний бэкап восстанавливается (roadmap 8.2): разворачивает
# дамп во временную базу restore_check, сравнивает число строк в таблицах с
# рабочей базой и удаляет временную. Рабочую базу НЕ трогает.
# Запуск: docker compose exec -T backup sh /ops/restore_check.sh
set -eu
latest=$(ls -1t /backups/ggsell_*.dump | head -1)
echo "Проверяю $latest"
psql -q -d postgres -c "DROP DATABASE IF EXISTS restore_check" -c "CREATE DATABASE restore_check"
pg_restore --no-owner -d restore_check "$latest"
status=0
for table in positions listings orders settings; do
    live=$(psql -At -c "SELECT count(*) FROM $table")
    restored=$(psql -At -d restore_check -c "SELECT count(*) FROM $table")
    mark=ok; [ "$live" = "$restored" ] || { mark="РАСХОЖДЕНИЕ"; status=1; }
    echo "$table: рабочая $live, из бэкапа $restored — $mark"
done
psql -q -d postgres -c "DROP DATABASE restore_check"
exit $status
