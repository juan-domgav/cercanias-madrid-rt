"""Bot de operaciones: allowlist, comandos, offset y buzón, sin red ni GCS."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import telegram_ops_poll as bot

TOKEN = "123456:SECRETO-NO-LOGUEAR"
ALLOWED = 5830900654
NOW = datetime(2026, 10, 16, 8, 0, tzinfo=UTC)


class FakeTelegram:
    def __init__(self, updates: list[dict[str, Any]]) -> None:
        self.updates = updates
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        self.calls.append((method, body))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": self.updates})
        return httpx.Response(200, json={"ok": True, "result": {}})

    def sent(self) -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == "sendMessage"]


def _msg(update_id: int, text: str, chat_id: int = ALLOWED) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _run(tmp_path: Path, updates: list[dict[str, Any]]) -> tuple[FakeTelegram, Any, Any]:
    fake = FakeTelegram(updates)
    private = LocalSnapshotPublisher(tmp_path / "privado")
    web = LocalSnapshotPublisher(tmp_path / "web")
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    bot.poll_once(bot.TelegramApi(TOKEN, client), private, web, {ALLOWED}, now=lambda: NOW)
    return fake, private, web


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("estado", "estado"),
        ("Status", "estado"),
        ("/estado@CercaniasOpsBot", "estado"),
        ("horarios", "horarios"),
        ("ayuda", "ayuda"),
        ("/help", "ayuda"),
        ("regenerar ya", "regenerar"),
        ("hola", None),
        ("", None),
    ],
)
def test_parse_command(text: str, expected: str | None) -> None:
    assert bot.parse_command(text) == expected


def test_parse_allowed_chat_ids() -> None:
    assert bot.parse_allowed_chat_ids("5830900654, -100, x,") == {5830900654, -100}


def test_chat_no_permitido_se_ignora_pero_avanza_offset(tmp_path: Path) -> None:
    fake, private, web = _run(tmp_path, [_msg(10, "estado", chat_id=999)])
    assert fake.sent() == []
    assert bot.load_offset(private) == 11
    assert web.read(bot.INBOX_KEY) is None


def test_offset_se_envia_y_persiste(tmp_path: Path) -> None:
    private = LocalSnapshotPublisher(tmp_path / "privado")
    bot.save_offset(private, 41)
    fake = FakeTelegram([_msg(42, "ayuda"), _msg(43, "help")])
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    web = LocalSnapshotPublisher(tmp_path / "web")
    bot.poll_once(bot.TelegramApi(TOKEN, client), private, web, {ALLOWED})
    assert fake.calls[0] == ("getUpdates", {"timeout": 0, "allowed_updates": ["message"], "offset": 41})
    assert bot.load_offset(private) == 44
    assert len(fake.sent()) == 2


def test_offset_corrupto_arranca_sin_offset(tmp_path: Path) -> None:
    private = LocalSnapshotPublisher(tmp_path)
    private.publish(bot.OFFSET_KEY, b"{no json", content_type="application/json")
    assert bot.load_offset(private) is None


def test_sin_updates_no_reescribe_offset(tmp_path: Path) -> None:
    _, private, _ = _run(tmp_path, [])
    assert private.read(bot.OFFSET_KEY) is None


def test_mensaje_desconocido_va_al_buzon_minimo(tmp_path: Path) -> None:
    fake, _, web = _run(tmp_path, [_msg(7, "el tablón de Sol va raro")])
    assert fake.sent()[0]["text"] == bot.ACK_UNKNOWN
    data = json.loads(web.read(bot.INBOX_KEY))
    assert data == {"items": [{"ts": NOW.isoformat(), "update_id": 7, "text": "el tablón de Sol va raro"}]}


def test_buzon_conserva_ultimos_20_y_no_duplica(tmp_path: Path) -> None:
    web = LocalSnapshotPublisher(tmp_path)
    for i in range(25):
        bot.append_inbox(web, {"ts": "t", "update_id": i, "text": str(i)})
    bot.append_inbox(web, {"ts": "t", "update_id": 24, "text": "24"})
    items = json.loads(web.read(bot.INBOX_KEY))["items"]
    assert len(items) == bot.INBOX_MAX_ITEMS
    assert items[0]["update_id"] == 5 and items[-1]["update_id"] == 24


def test_estado_y_horarios(tmp_path: Path) -> None:
    web = LocalSnapshotPublisher(tmp_path / "web")
    boards = {
        "generated_at": "2026-10-16T07:59:00+00:00",
        "service": "1099X",
        "stations_with_departures": 1,
        "health": {"schedule_missing": False, "schedule_days_left": 2, "no_departures": False},
        "stations": {"17000": {"departures": [{}, {}]}, "18000": {"departures": []}},
    }
    snap = {
        "generated_at": "2026-10-16T07:59:30+00:00",
        "vehicles": [{}, {}, {}],
        "degraded": True,
        "reason": "sin datos",
    }
    web.publish("boards.json", json.dumps(boards).encode(), content_type="application/json")
    web.publish("snapshot.json", json.dumps(snap).encode(), content_type="application/json")
    for s in ("1099X", "1001J", "1002V"):
        web.publish(f"horarios/{s}.json", b"{}", content_type="application/json")
    estado = bot.build_status(web, NOW)
    assert "servicio: 1099X" in estado
    assert "estaciones: 2" in estado and "salidas: 2" in estado
    assert "schedule_days_left=2" in estado and "trenes: 3" in estado
    assert "edad snapshot: 30 s" in estado and "degraded: True" in estado
    horarios = bot.build_horarios(web)
    assert "días por delante: 2" in horarios and "horarios/1002V.json" in horarios


def test_estado_sin_ficheros(tmp_path: Path) -> None:
    estado = bot.build_status(LocalSnapshotPublisher(tmp_path), NOW)
    assert "boards.json: NO DISPONIBLE" in estado and "snapshot.json: NO DISPONIBLE" in estado


def test_regenerar_no_ejecuta_nada(tmp_path: Path) -> None:
    fake, _, _ = _run(tmp_path, [_msg(1, "regenerar")])
    assert "gcloud run jobs execute cercanias-horarios" in fake.sent()[0]["text"]
    assert [m for m, _ in fake.calls] == ["getUpdates", "sendMessage"]


def test_error_de_api_no_filtra_el_token(capsys: pytest.CaptureFixture[str]) -> None:
    def falla(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"no conecta a {request.url}")

    api = bot.TelegramApi(TOKEN, httpx.Client(transport=httpx.MockTransport(falla)))
    with pytest.raises(RuntimeError) as exc:
        api.get_updates(None)
    assert TOKEN not in str(exc.value) and "<token>" in str(exc.value)


def test_main_sin_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_OPS_BOT_TOKEN", raising=False)
    assert bot.main() == 2
