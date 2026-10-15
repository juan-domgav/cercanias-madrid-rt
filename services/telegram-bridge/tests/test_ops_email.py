"""Unit tests del envío email ops (smtplib mockeado; sin red)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from app.ops_email import (
    DEFAULT_OPS_EMAIL_TO,
    build_ops_body,
    build_ops_subject,
    send_ops_email,
    smtp_configured,
)


def test_build_ops_subject_truncates_to_60() -> None:
    long = "a" * 100
    subject = build_ops_subject(long)
    assert subject.startswith("[telegram-ops] ")
    assert len(subject) == len("[telegram-ops] ") + 60


def test_build_ops_subject_strips_newlines() -> None:
    assert build_ops_subject("hola\nmundo") == "[telegram-ops] hola mundo"


def test_build_ops_body_includes_fields() -> None:
    body = build_ops_body(
        chat_id=5830900654,
        text="estado C5",
        from_user={"id": 1, "username": "obiguany", "first_name": "Juan"},
        update_id=99,
    )
    assert "chat_id: 5830900654" in body
    assert "username: obiguany" in body
    assert "update_id: 99" in body
    assert "estado C5" in body
    assert "source: telegram" in body


def test_smtp_configured() -> None:
    assert smtp_configured(host="smtp.example", mail_from="a@b.c") is True
    assert smtp_configured(host="", mail_from="a@b.c") is False
    assert smtp_configured(host="smtp.example", mail_from="") is False


def test_send_ops_email_skips_without_smtp() -> None:
    with patch("app.ops_email.smtplib.SMTP") as smtp_cls:
        send_ops_email(
            to_address=DEFAULT_OPS_EMAIL_TO,
            subject="[telegram-ops] x",
            body="body",
            smtp_host="",
            smtp_port=587,
            smtp_user="",
            smtp_password="",
            smtp_from="",
        )
        smtp_cls.assert_not_called()


def test_send_ops_email_uses_starttls_on_587() -> None:
    server = MagicMock()
    with patch("app.ops_email.smtplib.SMTP", return_value=server) as smtp_cls:
        server.__enter__.return_value = server
        send_ops_email(
            to_address=DEFAULT_OPS_EMAIL_TO,
            subject="[telegram-ops] estado",
            body="chat_id: 1\n",
            smtp_host="smtp.example.invalid",
            smtp_port=587,
            smtp_user="user",
            smtp_password="pass",
            smtp_from="bridge@example.invalid",
        )
        smtp_cls.assert_called_once_with("smtp.example.invalid", 587, timeout=15)
        server.starttls.assert_called_once()
        server.login.assert_called_once_with("user", "pass")
        server.send_message.assert_called_once()
        msg = server.send_message.call_args.args[0]
        assert msg["To"] == DEFAULT_OPS_EMAIL_TO
        assert msg["From"] == "bridge@example.invalid"
        assert msg["Subject"] == "[telegram-ops] estado"


def test_send_ops_email_uses_ssl_on_465() -> None:
    server = MagicMock()
    with patch("app.ops_email.smtplib.SMTP_SSL", return_value=server) as ssl_cls:
        server.__enter__.return_value = server
        send_ops_email(
            to_address="ops@example.test",
            subject="[telegram-ops] ping",
            body="x",
            smtp_host="smtp.example.invalid",
            smtp_port=465,
            smtp_user="user",
            smtp_password="pass",
            smtp_from="bridge@example.invalid",
        )
        ssl_cls.assert_called_once()
        assert ssl_cls.call_args.args[0] == "smtp.example.invalid"
        assert ssl_cls.call_args.args[1] == 465
        server.login.assert_called_once_with("user", "pass")
        server.send_message.assert_called_once()


def test_send_ops_email_logs_smtp_error(caplog: Any) -> None:
    import logging
    import smtplib

    server = MagicMock()
    server.__enter__.return_value = server
    server.starttls.side_effect = smtplib.SMTPException("boom")
    with patch("app.ops_email.smtplib.SMTP", return_value=server):
        with caplog.at_level(logging.ERROR, logger="telegram_bridge.ops_email"):
            send_ops_email(
                to_address=DEFAULT_OPS_EMAIL_TO,
                subject="[telegram-ops] x",
                body="y",
                smtp_host="smtp.example.invalid",
                smtp_port=587,
                smtp_user="u",
                smtp_password="p",
                smtp_from="bridge@example.invalid",
            )
    assert any("Error enviando email ops" in r.message for r in caplog.records)
