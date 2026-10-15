"""Tests HTTP del webhook y /reply (httpx/SMTP mockeados; sin red real)."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient


SECRET = "test-webhook-secret"


def test_healthz(client: TestClient) -> None:
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_webhook_rejects_bad_secret(client: TestClient) -> None:
    r = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "nope"},
        json={"update_id": 1, "message": {"chat": {"id": 111}, "text": "hola"}},
    )
    assert r.status_code == 401


def test_webhook_ignores_non_allowlisted_chat(client: TestClient) -> None:
    r = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={
            "update_id": 2,
            "message": {"chat": {"id": 999, "type": "private"}, "text": "hola"},
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_webhook_ignores_group_unless_allowlisted(client: TestClient) -> None:
    r = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={
            "update_id": 3,
            "message": {
                "chat": {"id": -1001, "type": "supergroup"},
                "text": "grupo",
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "ignored"


def test_webhook_accepts_and_forwards_email_and_webhook(
    client: TestClient, monkeypatch: Any
) -> None:
    email_called: dict[str, Any] = {}
    grok_called: dict[str, Any] = {}

    def _fake_email(**kwargs: Any) -> None:
        email_called.update(kwargs)

    async def _fake_forward(**kwargs: Any) -> None:
        grok_called.update(kwargs)

    monkeypatch.setattr("app.main.send_ops_email", _fake_email)
    monkeypatch.setattr("app.main.forward_to_grok", _fake_forward)

    r = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={
            "update_id": 42,
            "message": {
                "chat": {"id": 111, "type": "private"},
                "text": "estado",
                "from": {"id": 7, "username": "juan"},
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert email_called["to_address"] == "cercanias-ops@mail.grokbot.com"
    assert email_called["subject"] == "[telegram-ops] estado"
    assert "chat_id: 111" in email_called["body"]
    assert "username: juan" in email_called["body"]
    assert "update_id: 42" in email_called["body"]
    assert "estado" in email_called["body"]
    assert email_called["smtp_host"] == "smtp.example.invalid"
    assert grok_called["payload"]["chat_id"] == 111
    assert grok_called["payload"]["text"] == "estado"
    assert grok_called["payload"]["source"] == "telegram"
    assert grok_called["payload"]["update_id"] == 42
    assert grok_called["key"] == "test-grok-key"


def test_webhook_email_only_when_no_grok_webhook(
    client: TestClient, monkeypatch: Any
) -> None:
    email_called: dict[str, Any] = {}
    grok_called = {"n": 0}

    def _fake_email(**kwargs: Any) -> None:
        email_called.update(kwargs)

    async def _fake_forward(**kwargs: Any) -> None:
        grok_called["n"] += 1

    monkeypatch.setenv("GROK_WEBHOOK_URL", "")
    monkeypatch.setenv("GROK_WEBHOOK_KEY", "")
    monkeypatch.setattr("app.main.send_ops_email", _fake_email)
    monkeypatch.setattr("app.main.forward_to_grok", _fake_forward)

    r = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={
            "update_id": 7,
            "message": {
                "chat": {"id": 111, "type": "private"},
                "text": "solo email",
                "from": {"id": 7, "username": "obiguany"},
            },
        },
    )
    assert r.status_code == 200
    assert r.json()["status"] == "accepted"
    assert email_called["subject"] == "[telegram-ops] solo email"
    assert grok_called["n"] == 0


def test_reply_rejects_unknown_chat(client: TestClient) -> None:
    r = client.post(
        "/reply",
        headers={"X-Telegram-Bot-Api-Secret-Token": SECRET},
        json={"chat_id": 999, "text": "hola"},
    )
    assert r.status_code == 403


def test_reply_unauthorized(client: TestClient) -> None:
    r = client.post("/reply", json={"chat_id": 111, "text": "hola"})
    assert r.status_code == 401


def test_reply_sends_when_allowed(
    client: TestClient, monkeypatch: Any
) -> None:
    mock_send = AsyncMock(return_value={"ok": True, "result": {"message_id": 1}})
    monkeypatch.setattr("app.main.send_message", mock_send)

    r = client.post(
        "/reply",
        headers={"Authorization": f"Bearer {SECRET}"},
        json={"chat_id": 111, "text": "respuesta"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "sent"
    mock_send.assert_awaited_once()
    assert mock_send.await_args.kwargs["chat_id"] == 111
    assert mock_send.await_args.kwargs["text"] == "respuesta"
