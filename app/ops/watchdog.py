"""Сторож (roadmap 8.4): раз в 5 минут в scheduler проверяет, что всё живо, и
шлёт алерт в Telegram — один раз, когда проблема появилась, и ещё раз, когда
прошла (состояние — в settings, чтобы не слать одно и то же каждые 5 минут).

Проверки:
  - app отвечает на /health (вебхуки GGSell принимаются);
  - бот жив и достаёт до Telegram (сердцебиение < 10 мин) — заодно ловит
    истёкший прокси Telegram (notes 6.20);
  - синхронизация цен завершалась недавно (не старше интервала + 1.5 ч);
  - бэкап БД свежий (< 26 ч), если папка бэкапов подключена;
  - место на диске (> 85% занято — алерт);
  - перезапуски app/bot/scheduler (сменилась отметка запуска) — сообщение.

Чего сторож не поймает: падение всего сервера или самого scheduler — изнутри
об этом сообщить некому. Для этого — внешний мониторинг адреса /health/full
(UptimeRobot и т.п., docs/operations.md).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import httpx
from sqlalchemy.orm import Session

from app import settings
from app.ops.heartbeat import SERVICES, age_seconds, heartbeat_key, started_key

logger = logging.getLogger(__name__)

STATE_KEY = "watchdog_state"  # {"problems": {...}, "started": {...}}
BOT_STALE_SECONDS = 10 * 60
BACKUP_STALE_HOURS = 26
DISK_ALERT_PERCENT = 85


@dataclass
class Environment:
    """Внешний мир сторожа — в тестах подменяется."""

    app_health_url: str = os.getenv("WATCHDOG_APP_URL", "http://app:8000/health")
    backups_dir: Path = Path(os.getenv("BACKUP_DIR", "/backups"))
    disk_path: str = "/"
    sync_interval_hours: float = float(os.getenv("CATALOG_SYNC_INTERVAL_HOURS", "12"))
    bot_enabled: bool = True
    http_get: Callable[[str], httpx.Response] = lambda url: httpx.get(url, timeout=10)
    disk_usage: Callable[[str], tuple] = shutil.disk_usage


def check_problems(session: Session, env: Environment, now: Optional[datetime] = None) -> dict[str, str]:
    """{ключ проблемы: описание}. Пустой словарь — всё в порядке."""
    now = now or datetime.now(timezone.utc)
    problems: dict[str, str] = {}

    try:
        response = env.http_get(env.app_health_url)
        if response.status_code != 200:
            problems["app"] = f"сервис app (вебхуки GGSell) отвечает {response.status_code}"
    except httpx.HTTPError as e:
        problems["app"] = f"сервис app (вебхуки GGSell) не отвечает: {type(e).__name__}"

    if env.bot_enabled:
        bot_age = age_seconds(session, heartbeat_key("bot"), now)
        if bot_age is None or bot_age > BOT_STALE_SECONDS:
            minutes = "никогда" if bot_age is None else f"{int(bot_age // 60)} мин назад"
            problems["bot"] = (f"бот не связывался с Telegram ({minutes}) — бот упал или не работает "
                               "прокси Telegram (истёк срок?)")

    sync_age = age_seconds(session, settings.SYNC_FINISHED_AT, now)
    sync_limit = (env.sync_interval_hours + 1.5) * 3600
    if sync_age is not None and sync_age > sync_limit:
        problems["sync"] = f"синхронизация цен не завершалась {sync_age / 3600:.1f} ч"

    if env.backups_dir.is_dir():
        dumps = sorted(env.backups_dir.glob("ggsell_*.dump"), key=lambda p: p.stat().st_mtime)
        if not dumps:
            problems["backup"] = "в папке бэкапов нет ни одного дампа БД"
        else:
            backup_age = now.timestamp() - dumps[-1].stat().st_mtime
            if backup_age > BACKUP_STALE_HOURS * 3600:
                problems["backup"] = f"последний бэкап БД — {backup_age / 3600:.0f} ч назад"

    total, used, _free = env.disk_usage(env.disk_path)[:3]
    percent = used / total * 100 if total else 0
    if percent > DISK_ALERT_PERCENT:
        problems["disk"] = f"диск занят на {percent:.0f}%"

    return problems


def _load_state(session: Session) -> dict:
    raw = settings.get(session, STATE_KEY)
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {}


def run_watchdog(session: Session, env: Environment, alert: Callable[[str], None],
                 now: Optional[datetime] = None) -> dict[str, str]:
    state = _load_state(session)
    known: dict[str, str] = state.get("problems", {})
    problems = check_problems(session, env, now)

    new = {k: v for k, v in problems.items() if k not in known}
    resolved = [k for k in known if k not in problems]
    if new:
        alert("🚨 Проблема:\n" + "\n".join(f"  • {text}" for text in new.values()))
    if resolved:
        alert("✅ Восстановлено:\n" + "\n".join(f"  • {known[k]}" for k in resolved))

    # Перезапуски: отметка запуска сменилась с прошлой проверки.
    seen: dict[str, str] = state.get("started", {})
    current = {s: settings.get(session, started_key(s)) for s in SERVICES}
    restarted = [s for s, value in current.items() if value and seen.get(s) and value != seen[s]]
    if restarted:
        alert("🔄 Перезапущены сервисы: " + ", ".join(restarted)
              + " (деплой или падение — если не деплоили, посмотрите логи)")

    settings.set_value(session, STATE_KEY, json.dumps(
        {"problems": problems, "started": {s: v for s, v in current.items() if v}}, ensure_ascii=False))
    session.commit()
    if problems:
        logger.warning("Сторож: %s", problems)
    return problems
