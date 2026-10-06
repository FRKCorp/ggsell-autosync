"""Сердцебиения и отметки запуска сервисов (roadmap 8.4) — в таблице settings.

  - heartbeat_<service> — сервис жив (scheduler — раз в минуту, bot — после
    успешного опроса Telegram, не чаще раза в минуту). По ним сторож
    (watchdog.py) и /health/full понимают, что сервис не завис.
  - started_<service> — когда процесс запустился. Сменилось — сервис
    перезапускался (деплой или падение) — сторож об этом сообщает.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app import settings
from app.db import SessionLocal

SERVICES = ("app", "scheduler", "bot")


def heartbeat_key(service: str) -> str:
    return f"heartbeat_{service}"


def started_key(service: str) -> str:
    return f"started_{service}"


def _write(key: str, session: Optional[Session] = None) -> None:
    own = session is None
    session = session or SessionLocal()
    try:
        settings.touch(session, key)
        session.commit()
    finally:
        if own:
            session.close()


def beat(service: str, session: Optional[Session] = None) -> None:
    _write(heartbeat_key(service), session)


def mark_started(service: str, session: Optional[Session] = None) -> None:
    _write(started_key(service), session)
    _write(heartbeat_key(service), session)


def age_seconds(session: Session, key: str, now: Optional[datetime] = None) -> Optional[float]:
    value = settings.get_datetime(session, key)
    if value is None:
        return None
    now = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (now - value).total_seconds()
