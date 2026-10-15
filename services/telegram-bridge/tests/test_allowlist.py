"""Unit tests: allowlist y extracción de chat_id (sin red)."""

from __future__ import annotations

from app.allowlist import (
    extract_chat_id,
    extract_from_user,
    extract_message_text,
    is_chat_allowed,
)


def test_extract_chat_id_from_message() -> None:
    update = {"update_id": 1, "message": {"chat": {"id": 111, "type": "private"}, "text": "hola"}}
    assert extract_chat_id(update) == 111
    assert extract_message_text(update) == "hola"
    assert extract_from_user(update) is None


def test_extract_chat_id_group_and_channel() -> None:
    group = {"message": {"chat": {"id": -100123, "type": "supergroup"}, "text": "x"}}
    channel = {"channel_post": {"chat": {"id": -100999, "type": "channel"}, "text": "y"}}
    assert extract_chat_id(group) == -100123
    assert extract_chat_id(channel) == -100999


def test_is_chat_allowed_only_listed() -> None:
    allowed = frozenset({111, 222})
    assert is_chat_allowed(111, allowed) is True
    assert is_chat_allowed(999, allowed) is False
    assert is_chat_allowed(None, allowed) is False
    assert is_chat_allowed(111, frozenset()) is False


def test_group_not_allowed_unless_listed() -> None:
    """Grupos/canales se ignoran salvo que el chat.id esté en allowlist."""
    allowed = frozenset({111})
    group_id = -100555
    assert is_chat_allowed(group_id, allowed) is False
    assert is_chat_allowed(group_id, frozenset({group_id})) is True
