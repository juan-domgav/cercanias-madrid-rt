"""Configuración desde variables de entorno. Sin secretos por defecto."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _parse_chat_ids(raw: str) -> frozenset[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return frozenset(ids)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_webhook_secret: str
    allowed_chat_ids: frozenset[int]
    grok_webhook_url: str
    grok_webhook_key: str
    reply_auth_token: str | None
    grok_forward_timeout_seconds: float = 2.0

    @classmethod
    def from_env(cls) -> Settings:
        allowed = _parse_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", ""))
        reply = os.environ.get("REPLY_AUTH_TOKEN", "").strip() or None
        timeout_raw = os.environ.get("GROK_FORWARD_TIMEOUT_SECONDS", "2.0")
        return cls(
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            telegram_webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
            allowed_chat_ids=allowed,
            grok_webhook_url=os.environ.get("GROK_WEBHOOK_URL", ""),
            grok_webhook_key=os.environ.get("GROK_WEBHOOK_KEY", ""),
            reply_auth_token=reply,
            grok_forward_timeout_seconds=float(timeout_raw),
        )


def load_settings() -> Settings:
    return Settings.from_env()
