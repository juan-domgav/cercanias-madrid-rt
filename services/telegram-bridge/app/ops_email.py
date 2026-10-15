"""Aviso por email a la inbox de Grok Bot (SMTP vía env). Sin deps extra."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

logger = logging.getLogger("telegram_bridge.ops_email")

DEFAULT_OPS_EMAIL_TO = "cercanias-ops@mail.grokbot.com"


def build_ops_subject(text: str | None) -> str:
    snippet = (text or "").replace("\n", " ").strip()[:60]
    return f"[telegram-ops] {snippet}"


def build_ops_body(
    *,
    chat_id: int,
    text: str | None,
    from_user: dict[str, Any] | None,
    update_id: int | None,
) -> str:
    username = ""
    if isinstance(from_user, dict):
        username = str(from_user.get("username") or "")
    lines = [
        "source: telegram",
        f"chat_id: {chat_id}",
        f"username: {username}",
        f"update_id: {update_id if update_id is not None else ''}",
        "text:",
        text or "",
    ]
    return "\n".join(lines) + "\n"


def smtp_configured(*, host: str, mail_from: str) -> bool:
    return bool(host.strip()) and bool(mail_from.strip())


def send_ops_email(
    *,
    to_address: str,
    subject: str,
    body: str,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    smtp_from: str,
) -> None:
    """Envía el aviso por SMTP. Fire-and-forget: errores solo se loguean.

    Puerto 465 → SMTP_SSL; resto → SMTP + STARTTLS (típico 587).
    """
    if not smtp_configured(host=smtp_host, mail_from=smtp_from):
        logger.warning("SMTP_HOST/SMTP_FROM ausentes; no se envía email ops")
        return

    to_addr = (to_address or DEFAULT_OPS_EMAIL_TO).strip() or DEFAULT_OPS_EMAIL_TO
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from.strip()
    msg["To"] = to_addr
    msg.set_content(body)

    context = ssl.create_default_context()
    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(
                smtp_host, smtp_port, timeout=15, context=context
            ) as server:
                if smtp_user:
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                if smtp_user:
                    server.login(smtp_user, smtp_password)
                server.send_message(msg)
        logger.info("Email ops enviado a %s subject=%r", to_addr, subject[:80])
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        logger.error("Error enviando email ops: %s", exc)
