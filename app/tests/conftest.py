import pytest


@pytest.fixture(autouse=True)
def no_real_telegram(monkeypatch):
    """Тесты не должны слать алерты в настоящий Telegram, даже если токен
    есть в локальном .env (load_dotenv не перетирает уже заданные
    переменные окружения)."""
    monkeypatch.setenv("ADMIN_TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "")
