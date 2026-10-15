"""Cliente mínimo de la Bot API (solo sendMessage)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("telegram_bridge.telegram")

TELEGRAM_API_BASE = "https://api.telegram.org"


async def send_message(
    *,
    bot_token: str,
    chat_id: int,
    text: str,
    timeout_seconds: float = 15.0,
) -> dict[str, Any]:
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN no configurado")
    url = f"{TELEGRAM_API_BASE}/bot{bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=timeout_seconds) as client:
        response = await client.post(
            url,
            json={"chat_id": chat_id, "text": text},
        )
        body = response.json() if response.content else {}
        if response.status_code >= 400 or not body.get("ok", False):
            # No incluir el token en logs
            logger.error(
                "sendMessage falló status=%s description=%s",
                response.status_code,
                body.get("description"),
            )
            raise RuntimeError(
                f"Telegram sendMessage falló: {body.get('description', response.status_code)}"
            )
        return body
