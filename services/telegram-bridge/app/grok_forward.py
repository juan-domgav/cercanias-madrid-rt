"""Reenvío fire-and-forget al webhook de una rutina Grok Bot."""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger("telegram_bridge.grok")


def build_grok_payload(
    *,
    chat_id: int,
    text: str | None,
    from_user: dict[str, Any] | None,
    update_id: int | None,
) -> dict[str, Any]:
    return {
        "source": "telegram",
        "chat_id": chat_id,
        "text": text or "",
        "from": from_user,
        "update_id": update_id,
    }


async def forward_to_grok(
    *,
    url: str,
    key: str,
    payload: dict[str, Any],
    timeout_seconds: float,
) -> None:
    """POST al webhook de Grok. Authorization: Bearer (docs de Routines).

    Timeout corto a propósito: Telegram ya recibió 200; un fallo aquí solo se loguea.
    """
    if not url or not key:
        logger.warning("GROK_WEBHOOK_URL o GROK_WEBHOOK_KEY ausentes; no se reenvía")
        return

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code >= 400:
                logger.error(
                    "Grok webhook respondió %s: %s",
                    response.status_code,
                    response.text[:200],
                )
            else:
                logger.info("Grok webhook aceptó update_id=%s", payload.get("update_id"))
    except httpx.TimeoutException:
        logger.warning(
            "Timeout (%ss) reenviando a Grok update_id=%s",
            timeout_seconds,
            payload.get("update_id"),
        )
    except httpx.HTTPError as exc:
        logger.error("Error HTTP reenviando a Grok: %s", exc)
