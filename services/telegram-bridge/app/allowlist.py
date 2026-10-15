"""Filtrado de chat_id frente a ALLOWED_CHAT_IDS."""

from __future__ import annotations

from typing import Any


def extract_chat_id(update: dict[str, Any]) -> int | None:
    """Obtiene chat.id del Update de Telegram (mensaje, edición o canal)."""
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        block = update.get(key)
        if isinstance(block, dict):
            chat = block.get("chat")
            if isinstance(chat, dict) and "id" in chat:
                try:
                    return int(chat["id"])
                except (TypeError, ValueError):
                    return None
    # Callback / otros: chat anidado en message
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        message = callback.get("message")
        if isinstance(message, dict):
            chat = message.get("chat")
            if isinstance(chat, dict) and "id" in chat:
                try:
                    return int(chat["id"])
                except (TypeError, ValueError):
                    return None
    return None


def extract_message_text(update: dict[str, Any]) -> str | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        block = update.get(key)
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                return text
            caption = block.get("caption")
            if isinstance(caption, str):
                return caption
    return None


def extract_from_user(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message", "channel_post", "edited_channel_post"):
        block = update.get(key)
        if isinstance(block, dict):
            user = block.get("from")
            if isinstance(user, dict):
                return {
                    "id": user.get("id"),
                    "username": user.get("username"),
                    "first_name": user.get("first_name"),
                }
    callback = update.get("callback_query")
    if isinstance(callback, dict):
        user = callback.get("from")
        if isinstance(user, dict):
            return {
                "id": user.get("id"),
                "username": user.get("username"),
                "first_name": user.get("first_name"),
            }
    return None


def is_chat_allowed(chat_id: int | None, allowed: frozenset[int]) -> bool:
    """Solo chats explícitamente en la allowlist (DM, grupo o canal)."""
    if chat_id is None:
        return False
    if not allowed:
        return False
    return chat_id in allowed
