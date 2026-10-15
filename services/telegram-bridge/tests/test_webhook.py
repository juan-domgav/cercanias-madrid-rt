"""Tests HTTP del webhook y /reply (httpx mockeado; sin red real)."""

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


def test_webhook_accepts_and_forwards(
    client: TestClient, monkeypatch: Any
) -> None:
    called: dict[str, Any] = {}

    async def _fake_forward(**kwargs: Any) -> None:
        called.update(kwargs)

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
    assert called["payload"]["chat_id"] == 111
    assert called["payload"]["text"] == "estado"
    assert called["payload"]["source"] == "telegram"
    assert called["payload"]["update_id"] == 42
    assert called["key"] == "test-grok-key"


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
