"""Минимальный синхронный клиент Telegram Bot API (roadmap этап 6).

Своя обёртка над httpx, а не python-telegram-bot/aiogram: нужно несколько
методов (sendMessage, editMessageText, answerCallbackQuery, getUpdates),
весь остальной код проекта синхронный (SQLAlchemy, клиенты GGSell/FZ) —
асинхронный фреймворк ради этого не тянем.

Токен и chat_id — только из .env (требование клиента 9.2a):
ADMIN_TELEGRAM_BOT_TOKEN, ADMIN_TELEGRAM_CHAT_ID (можно несколько через
запятую — алерты уходят всем, бот слушается всех).
"""

from __future__ import annotations

import os
from typing import Any, Optional

import httpx
from dotenv import load_dotenv

API_URL = "https://api.telegram.org"
MAX_MESSAGE_LENGTH = 4096


class TelegramError(Exception):
    def __init__(self, description: str, payload: Any = None):
        super().__init__(description)
        self.payload = payload


def admin_chat_ids() -> list[int]:
    load_dotenv()
    raw = os.getenv("ADMIN_TELEGRAM_CHAT_ID", "")
    ids = []
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            ids.append(int(part))
    return ids


def bot_token() -> Optional[str]:
    load_dotenv()
    token = os.getenv("ADMIN_TELEGRAM_BOT_TOKEN", "").strip()
    # Заглушка из .env.example — не токен.
    return token if token and ":" in token else None


def truncate(text: str, limit: int = MAX_MESSAGE_LENGTH) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n… (обрезано)"


class TelegramClient:
    def __init__(self, token: str, timeout: float = 15.0, base_url: str = API_URL):
        # Telegram заблокирован в РФ — с российского VPS только через прокси
        # (notes 6.20): TELEGRAM_PROXY в .env, http://логин:пароль@хост:порт.
        # Прокси только у этого клиента — GGSell, FZ и ЦБ ходят напрямую;
        # системные HTTP(S)_PROXY не используем.
        load_dotenv()
        proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None
        self._client = httpx.Client(base_url=f"{base_url}/bot{token}", timeout=timeout, proxy=proxy)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "TelegramClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _call(self, method: str, http_timeout: Optional[float] = None, **params: Any) -> Any:
        body = {k: v for k, v in params.items() if v is not None}
        kwargs = {"timeout": http_timeout} if http_timeout is not None else {}
        response = self._client.post(f"/{method}", json=body, **kwargs)
        data = response.json()
        if not data.get("ok"):
            raise TelegramError(data.get("description", f"HTTP {response.status_code}"), data)
        return data["result"]

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[dict] = None,
                     reply_to_message_id: Optional[int] = None) -> dict:
        return self._call("sendMessage", chat_id=chat_id, text=truncate(text), reply_markup=reply_markup,
                          reply_to_message_id=reply_to_message_id, disable_web_page_preview=True)

    def edit_message_text(self, chat_id: int, message_id: int, text: str,
                          reply_markup: Optional[dict] = None) -> dict:
        return self._call("editMessageText", chat_id=chat_id, message_id=message_id, text=truncate(text),
                          reply_markup=reply_markup, disable_web_page_preview=True)

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> Any:
        return self._call("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 30) -> list[dict]:
        """Long polling: Telegram держит запрос до timeout секунд."""
        return self._call("getUpdates", http_timeout=timeout + 10, offset=offset, timeout=timeout,
                          allowed_updates=["message", "callback_query"])

    def set_my_commands(self, commands: list[tuple[str, str]]) -> Any:
        return self._call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])
