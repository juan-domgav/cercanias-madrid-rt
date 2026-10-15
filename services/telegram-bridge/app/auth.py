"""Comprobación de secretos de entrada (Telegram webhook y /reply)."""

from __future__ import annotations

import secrets


def secret_matches(provided: str | None, expected: str) -> bool:
    """Comparación en tiempo constante. Vacío esperado → rechaza siempre."""
    if not expected:
        return False
    if provided is None:
        return False
    return secrets.compare_digest(provided, expected)


def telegram_webhook_authorized(
    header_value: str | None, telegram_webhook_secret: str
) -> bool:
    """Telegram envía X-Telegram-Bot-Api-Secret-Token al setWebhook."""
    return secret_matches(header_value, telegram_webhook_secret)


def reply_authorized(
    *,
    secret_header: str | None,
    authorization: str | None,
    reply_token_header: str | None,
    telegram_webhook_secret: str,
    reply_auth_token: str | None,
) -> bool:
    """Autoriza /reply con TELEGRAM_WEBHOOK_SECRET o REPLY_AUTH_TOKEN.

    Acepta:
    - X-Telegram-Bot-Api-Secret-Token == TELEGRAM_WEBHOOK_SECRET
    - X-Reply-Auth-Token == REPLY_AUTH_TOKEN (si está definido) o TELEGRAM_WEBHOOK_SECRET
    - Authorization: Bearer <token> con cualquiera de los dos secretos válidos
    """
    accepted: list[str] = []
    if telegram_webhook_secret:
        accepted.append(telegram_webhook_secret)
    if reply_auth_token:
        accepted.append(reply_auth_token)

    if not accepted:
        return False

    candidates: list[str] = []
    if secret_header:
        candidates.append(secret_header)
    if reply_token_header:
        candidates.append(reply_token_header)
    if authorization and authorization.lower().startswith("bearer "):
        candidates.append(authorization[7:].strip())

    for candidate in candidates:
        for expected in accepted:
            if secret_matches(candidate, expected):
                return True
    return False
