"""Fixtures compartidos: sin red."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

# Valores de prueba; nunca secretos reales.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-bot-token")
os.environ.setdefault("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("ALLOWED_CHAT_IDS", "111,222")
os.environ.setdefault("GROK_WEBHOOK_URL", "https://example.invalid/grok")
os.environ.setdefault("GROK_WEBHOOK_KEY", "test-grok-key")
os.environ.setdefault("OPS_EMAIL_TO", "cercanias-ops@mail.grokbot.com")
os.environ.setdefault("SMTP_HOST", "smtp.example.invalid")
os.environ.setdefault("SMTP_PORT", "587")
os.environ.setdefault("SMTP_USER", "smtp-user")
os.environ.setdefault("SMTP_PASSWORD", "smtp-pass")
os.environ.setdefault("SMTP_FROM", "bridge@example.invalid")
os.environ.pop("REPLY_AUTH_TOKEN", None)


@pytest.fixture()
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-bot-token")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "111,222")
    monkeypatch.setenv("GROK_WEBHOOK_URL", "https://example.invalid/grok")
    monkeypatch.setenv("GROK_WEBHOOK_KEY", "test-grok-key")
    monkeypatch.setenv("OPS_EMAIL_TO", "cercanias-ops@mail.grokbot.com")
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_USER", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-pass")
    monkeypatch.setenv("SMTP_FROM", "bridge@example.invalid")
    monkeypatch.delenv("REPLY_AUTH_TOKEN", raising=False)

    from app.main import app

    return TestClient(app)
