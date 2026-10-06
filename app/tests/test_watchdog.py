import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app import settings
from app.models.base import Base
from app.ops.heartbeat import heartbeat_key, started_key
from app.ops.watchdog import Environment, check_problems, run_watchdog

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
GB = 1024 ** 3


@pytest.fixture
def session():
    # StaticPool — одно соединение: /health/full закрывает сессию, а без него
    # SQLite в памяти открыл бы новую пустую базу.
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def at(session, key, delta):
    settings.set_value(session, key, (NOW - delta).isoformat())


def healthy(session):
    at(session, heartbeat_key("bot"), timedelta(minutes=1))
    at(session, settings.SYNC_FINISHED_AT, timedelta(hours=2))
    session.commit()


@pytest.fixture
def env(tmp_path):
    dump = tmp_path / "ggsell_20261007_003000.dump"
    dump.write_bytes(b"x")
    os.utime(dump, ((NOW - timedelta(hours=11)).timestamp(),) * 2)
    return Environment(
        app_health_url="http://app/health",
        backups_dir=tmp_path,
        sync_interval_hours=12,
        bot_enabled=True,
        http_get=lambda url: httpx.Response(200),
        disk_usage=lambda path: (100 * GB, 50 * GB, 50 * GB),
    )


def test_all_good(session, env):
    healthy(session)
    assert check_problems(session, env, NOW) == {}


def test_app_down(session, env):
    healthy(session)

    def refuse(url):
        raise httpx.ConnectError("refused")

    env.http_get = refuse
    assert "не отвечает" in check_problems(session, env, NOW)["app"]


def test_bot_stale_or_never_and_disabled(session, env):
    healthy(session)
    at(session, heartbeat_key("bot"), timedelta(minutes=25))
    assert "25 мин назад" in check_problems(session, env, NOW)["bot"]

    settings.set_value(session, heartbeat_key("bot"), "")
    assert "никогда" in check_problems(session, env, NOW)["bot"]

    env.bot_enabled = False  # токена нет — бот не проверяем
    assert "bot" not in check_problems(session, env, NOW)


def test_sync_stale_and_never_ran_is_ok(session, env):
    healthy(session)
    at(session, settings.SYNC_FINISHED_AT, timedelta(hours=14))
    assert "14.0 ч" in check_problems(session, env, NOW)["sync"]

    settings.set_value(session, settings.SYNC_FINISHED_AT, "")
    assert "sync" not in check_problems(session, env, NOW)  # ещё ни разу не шла — не тревога


def test_backup_stale_missing_and_not_mounted(session, env, tmp_path):
    healthy(session)
    dump = next(tmp_path.glob("*.dump"))
    os.utime(dump, ((NOW - timedelta(hours=30)).timestamp(),) * 2)
    assert "30 ч назад" in check_problems(session, env, NOW)["backup"]

    dump.unlink()
    assert "нет ни одного" in check_problems(session, env, NOW)["backup"]

    env.backups_dir = tmp_path / "not-mounted"
    assert "backup" not in check_problems(session, env, NOW)


def test_disk_almost_full(session, env):
    healthy(session)
    env.disk_usage = lambda path: (100 * GB, 91 * GB, 9 * GB)
    assert check_problems(session, env, NOW)["disk"] == "диск занят на 91%"


def test_alert_once_then_recovered(session, env):
    healthy(session)
    alerts = []
    env.http_get = lambda url: httpx.Response(502)

    run_watchdog(session, env, alerts.append, NOW)
    run_watchdog(session, env, alerts.append, NOW)  # та же проблема — молчим

    assert len(alerts) == 1 and "🚨" in alerts[0] and "502" in alerts[0]

    env.http_get = lambda url: httpx.Response(200)
    run_watchdog(session, env, alerts.append, NOW)

    assert len(alerts) == 2 and "✅ Восстановлено" in alerts[1] and "502" in alerts[1]


def test_restart_reported(session, env):
    healthy(session)
    alerts = []
    settings.set_value(session, started_key("app"), "2026-10-07T10:00:00+00:00")
    settings.set_value(session, started_key("bot"), "2026-10-07T10:00:00+00:00")
    session.commit()
    run_watchdog(session, env, alerts.append, NOW)  # первая проверка — только запоминаем
    assert alerts == []

    settings.set_value(session, started_key("app"), "2026-10-07T11:58:00+00:00")
    session.commit()
    run_watchdog(session, env, alerts.append, NOW)

    assert len(alerts) == 1 and "Перезапущены сервисы: app" in alerts[0]


def test_health_full(monkeypatch, session):
    from fastapi.testclient import TestClient

    import app.api.main as api

    monkeypatch.setattr(api, "SessionLocal", lambda: session)
    monkeypatch.setattr(api, "mark_started", lambda service: None)
    client = TestClient(api.app)

    assert client.get("/health/full").status_code == 503  # scheduler ни разу не отметился

    settings.touch(session, heartbeat_key("scheduler"))
    session.commit()
    response = client.get("/health/full")
    assert response.status_code == 200 and response.json()["db"] == "ok"
