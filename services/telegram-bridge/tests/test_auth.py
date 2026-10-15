"""Unit tests: comprobación de secretos (sin red)."""

from __future__ import annotations

from app.auth import reply_authorized, secret_matches, telegram_webhook_authorized


def test_secret_matches_constant_time() -> None:
    assert secret_matches("abc", "abc") is True
    assert secret_matches("abc", "abd") is False
    assert secret_matches(None, "abc") is False
    assert secret_matches("abc", "") is False


def test_telegram_webhook_authorized() -> None:
    assert telegram_webhook_authorized("sekrit", "sekrit") is True
    assert telegram_webhook_authorized("wrong", "sekrit") is False
    assert telegram_webhook_authorized(None, "sekrit") is False


def test_reply_authorized_with_webhook_secret() -> None:
    assert (
        reply_authorized(
            secret_header="sekrit",
            authorization=None,
            reply_token_header=None,
            telegram_webhook_secret="sekrit",
            reply_auth_token=None,
        )
        is True
    )


def test_reply_authorized_bearer_and_reply_token() -> None:
    assert (
        reply_authorized(
            secret_header=None,
            authorization="Bearer reply-tok",
            reply_token_header=None,
            telegram_webhook_secret="sekrit",
            reply_auth_token="reply-tok",
        )
        is True
    )
    assert (
        reply_authorized(
            secret_header=None,
            authorization=None,
            reply_token_header="reply-tok",
            telegram_webhook_secret="sekrit",
            reply_auth_token="reply-tok",
        )
        is True
    )
    assert (
        reply_authorized(
            secret_header=None,
            authorization="Bearer wrong",
            reply_token_header=None,
            telegram_webhook_secret="sekrit",
            reply_auth_token="reply-tok",
        )
        is False
    )
