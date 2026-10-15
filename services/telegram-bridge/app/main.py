"""Servicio Cloud Run: webhook Telegram → email ops Grok (+ webhook opcional, /reply)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from app.allowlist import (
    extract_chat_id,
    extract_from_user,
    extract_message_text,
    is_chat_allowed,
)
from app.auth import reply_authorized, telegram_webhook_authorized
from app.config import Settings, load_settings
from app.grok_forward import build_grok_payload, forward_to_grok
from app.ops_email import build_ops_body, build_ops_subject, send_ops_email
from app.telegram_api import send_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("telegram_bridge")

app = FastAPI(title="telegram-bridge", version="0.2.0")


def get_settings() -> Settings:
    return load_settings()


class ReplyBody(BaseModel):
    chat_id: int
    text: str = Field(min_length=1, max_length=4096)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _queue_ops_notify(
    background_tasks: BackgroundTasks,
    settings: Settings,
    payload: dict[str, Any],
) -> None:
    """Email a la inbox Grok (primario) + webhook de rutina si está configurado."""
    background_tasks.add_task(
        send_ops_email,
        to_address=settings.ops_email_to,
        subject=build_ops_subject(payload.get("text")),
        body=build_ops_body(
            chat_id=int(payload["chat_id"]),
            text=payload.get("text"),
            from_user=payload.get("from"),
            update_id=payload.get("update_id"),
        ),
        smtp_host=settings.smtp_host,
        smtp_port=settings.smtp_port,
        smtp_user=settings.smtp_user,
        smtp_password=settings.smtp_password,
        smtp_from=settings.smtp_from,
    )
    if settings.grok_webhook_url and settings.grok_webhook_key:
        background_tasks.add_task(
            forward_to_grok,
            url=settings.grok_webhook_url,
            key=settings.grok_webhook_key,
            payload=payload,
            timeout_seconds=settings.grok_forward_timeout_seconds,
        )


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict[str, str]:
    settings = get_settings()
    if not telegram_webhook_authorized(
        x_telegram_bot_api_secret_token, settings.telegram_webhook_secret
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    try:
        update: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json") from None

    chat_id = extract_chat_id(update)
    if not is_chat_allowed(chat_id, settings.allowed_chat_ids):
        # 200 para no reintentos de Telegram; solo se ignora
        logger.info("Update ignorado chat_id=%s (fuera de allowlist)", chat_id)
        return {"status": "ignored"}

    assert chat_id is not None
    text = extract_message_text(update)
    if text is None and "callback_query" not in update:
        logger.info("Update sin texto chat_id=%s update_id=%s", chat_id, update.get("update_id"))
        return {"status": "ignored"}

    payload = build_grok_payload(
        chat_id=chat_id,
        text=text,
        from_user=extract_from_user(update),
        update_id=update.get("update_id"),
    )
    _queue_ops_notify(background_tasks, settings, payload)
    return {"status": "accepted"}


@app.post("/reply")
async def reply(
    body: ReplyBody,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    x_reply_auth_token: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    settings = get_settings()
    if not reply_authorized(
        secret_header=x_telegram_bot_api_secret_token,
        authorization=authorization,
        reply_token_header=x_reply_auth_token,
        telegram_webhook_secret=settings.telegram_webhook_secret,
        reply_auth_token=settings.reply_auth_token,
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    if not is_chat_allowed(body.chat_id, settings.allowed_chat_ids):
        raise HTTPException(status_code=403, detail="chat_id not allowed")

    result = await send_message(
        bot_token=settings.telegram_bot_token,
        chat_id=body.chat_id,
        text=body.text,
    )
    return {"status": "sent", "telegram": result.get("result")}
