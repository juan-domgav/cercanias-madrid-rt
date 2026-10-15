"""Configuración SMTP / OPS_EMAIL_TO desde env."""

from __future__ import annotations

from app.config import Settings
from app.ops_email import DEFAULT_OPS_EMAIL_TO


def test_settings_smtp_and_ops_email_defaults(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "5830900654")
    monkeypatch.delenv("OPS_EMAIL_TO", raising=False)
    monkeypatch.delenv("GROK_WEBHOOK_URL", raising=False)
    monkeypatch.delenv("GROK_WEBHOOK_KEY", raising=False)
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("SMTP_PORT", "465")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "p")
    monkeypatch.setenv("SMTP_FROM", "from@test")

    s = Settings.from_env()
    assert s.allowed_chat_ids == frozenset({5830900654})
    assert s.ops_email_to == DEFAULT_OPS_EMAIL_TO
    assert s.smtp_host == "smtp.test"
    assert s.smtp_port == 465
    assert s.smtp_password == "p"
    assert s.grok_webhook_url == ""
    assert s.grok_webhook_key == ""


def test_settings_smtp_pass_alias(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s")
    monkeypatch.setenv("ALLOWED_CHAT_IDS", "1")
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("SMTP_PASS", "alias-pass")
    monkeypatch.setenv("SMTP_HOST", "h")
    monkeypatch.setenv("SMTP_FROM", "f@t")

    s = Settings.from_env()
    assert s.smtp_password == "alias-pass"
