"""Webhook del bot de ops: secreto, enrutado y la misma lógica que el job."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from cercanias_api import main
from fastapi.testclient import TestClient

from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import telegram_ops_poll as ops

OPS_SECRET = "ops-secreto-de-prueba"
MAIN_SECRET = "bot-publico-secreto"
TOKEN = "123456:TOKEN-DE-PRUEBA-NO-LOGUEAR"
ALLOWED = 5830900654
URL = "/telegram/ops-webhook"


def _update(update_id: int, text: str, chat_id: int = ALLOWED) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _h(secret: str) -> dict[str, str]:
    return {"X-Telegram-Bot-Api-Secret-Token": secret}


#: Cabecera con bytes no ASCII (Starlette la decodifica como latin-1).
NO_ASCII: Any = {"X-Telegram-Bot-Api-Secret-Token": "ñ".encode() * 5}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(main, "OPS_WEBHOOK_SECRET", OPS_SECRET)
    monkeypatch.setattr(main, "WEBHOOK_SECRET", MAIN_SECRET)
    monkeypatch.setattr(main, "OPS_BOT_TOKEN", TOKEN)
    monkeypatch.setattr(main, "OPS_ALLOWED_CHAT_IDS", {ALLOWED})
    return TestClient(main.app)


@pytest.fixture
def handled(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Sustituye el manejador para comprobar solo el enrutado."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(main, "handle_ops_update", calls.append)
    return calls


# --- secreto -----------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [{}, _h(""), _h("otro"), _h(OPS_SECRET + "x"), _h(OPS_SECRET[:-1]), _h(MAIN_SECRET), NO_ASCII],
    ids=["sin-cabecera", "vacia", "incorrecta", "mas-larga", "mas-corta", "secreto-del-bot-publico",
         "no-ascii"],
)
def test_secreto_ausente_o_incorrecto_es_403(
    client: TestClient, handled: list[dict[str, Any]], headers: dict[str, str]
) -> None:
    r = client.post(URL, json=_update(1, "estado"), headers=headers)
    assert r.status_code == 403
    assert handled == []


def test_sin_secreto_configurado_rechaza_incluso_cabecera_vacia(
    client: TestClient, handled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "OPS_WEBHOOK_SECRET", "")
    for headers in ({}, _h("")):
        assert client.post(URL, json=_update(1, "estado"), headers=headers).status_code == 403
    assert handled == []


def test_secreto_correcto_enruta_al_manejador_de_ops(
    client: TestClient, handled: list[dict[str, Any]]
) -> None:
    update = _update(7, "estado")
    r = client.post(URL, json=update, headers=_h(OPS_SECRET))
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    assert handled == [update]


def test_el_secreto_de_ops_no_abre_el_webhook_publico(
    client: TestClient, handled: list[dict[str, Any]]
) -> None:
    r = client.post("/telegram/webhook", json=_update(1, "/start"), headers=_h(OPS_SECRET))
    assert r.status_code == 403
    assert handled == []


def test_webhook_publico_no_ascii_es_403_y_no_500(client: TestClient) -> None:
    r = client.post("/telegram/webhook", json=_update(1, "/start"), headers=NO_ASCII)
    assert r.status_code == 403


@pytest.mark.parametrize("body", [b"{no json", b"[1, 2]", b"null"])
def test_cuerpo_invalido_con_secreto_correcto_es_400(
    client: TestClient, handled: list[dict[str, Any]], body: bytes
) -> None:
    r = client.post(URL, content=body, headers={**_h(OPS_SECRET), "content-type": "application/json"})
    assert r.status_code == 400
    assert handled == []


def test_sin_token_responde_200_sin_procesar(
    client: TestClient, handled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main, "OPS_BOT_TOKEN", "")
    r = client.post(URL, json=_update(1, "estado"), headers=_h(OPS_SECRET))
    assert r.status_code == 200
    assert handled == []


# --- lógica real del job, de punta a punta (sin red ni GCS) ------------------


class _FakeTelegram:
    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.fail = fail

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((path.rsplit("/", 1)[-1], path, json.loads(request.content)))
        if self.fail:
            return httpx.Response(500, json={"ok": False, "description": "boom"})
        return httpx.Response(200, json={"ok": True, "result": {}})

    def sent(self) -> list[dict[str, Any]]:
        return [b for m, _, b in self.calls if m == "sendMessage"]


@pytest.fixture
def e2e(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher]:
    fake = _FakeTelegram()
    web = LocalSnapshotPublisher(tmp_path)
    monkeypatch.setattr(main, "publisher_from_env", lambda: web)
    real_client = httpx.Client

    def _client(*_a: Any, **_k: Any) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(fake.handler))

    monkeypatch.setattr(main.httpx, "Client", _client)
    return client, fake, web


def test_mensaje_libre_va_al_buzon_y_se_contesta(
    e2e: tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher],
) -> None:
    client, fake, web = e2e
    r = client.post(URL, json=_update(42, "revisa la C4"), headers=_h(OPS_SECRET))
    assert r.status_code == 200
    items = json.loads(web.read(ops.INBOX_KEY) or b"")["items"]
    assert [(i["update_id"], i["text"]) for i in items] == [(42, "revisa la C4")]
    assert "chat_id" not in items[0]
    assert fake.sent() == [{"chat_id": ALLOWED, "text": ops.ACK_UNKNOWN, "disable_web_page_preview": True}]
    # El token del bot de ops, nunca el del bot público.
    assert all(path.startswith(f"/bot{TOKEN}/") for _, path, _ in fake.calls)


def test_comando_regenerar_solo_devuelve_el_runbook(
    e2e: tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher],
) -> None:
    client, fake, web = e2e
    client.post(URL, json=_update(43, "/regenerar"), headers=_h(OPS_SECRET))
    assert [b["text"] for b in fake.sent()] == [ops.RUNBOOK_REGENERAR]
    assert [m for m, _, _ in fake.calls] == ["sendMessage"]
    assert web.read(ops.INBOX_KEY) is None


def test_estado_lee_el_bucket(
    e2e: tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher],
) -> None:
    client, fake, web = e2e
    now = datetime.now(tz=UTC).isoformat()
    web.publish("boards.json", json.dumps({"generated_at": now, "service": "1020L", "stations": {}}).encode(),
                content_type="application/json")
    web.publish("snapshot.json", json.dumps({"generated_at": now, "vehicles": [{}]}).encode(),
                content_type="application/json")
    client.post(URL, json=_update(44, "estado"), headers=_h(OPS_SECRET))
    texto = fake.sent()[0]["text"]
    assert "servicio: 1020L" in texto
    assert "trenes: 1" in texto


def test_chat_no_permitido_no_contesta_ni_escribe(
    e2e: tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher],
) -> None:
    client, fake, web = e2e
    r = client.post(URL, json=_update(45, "hola", chat_id=999), headers=_h(OPS_SECRET))
    assert r.status_code == 200
    assert fake.calls == []
    assert web.read(ops.INBOX_KEY) is None


def test_reentrega_del_mismo_update_no_duplica_el_buzon(
    e2e: tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher],
) -> None:
    client, _, web = e2e
    for _ in range(2):
        client.post(URL, json=_update(46, "hola"), headers=_h(OPS_SECRET))
    assert len(json.loads(web.read(ops.INBOX_KEY) or b"")["items"]) == 1


def test_fallo_de_telegram_responde_200_y_no_filtra_el_token(
    e2e: tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher],
    capsys: pytest.CaptureFixture[str],
) -> None:
    client, fake, _ = e2e
    fake.fail = True
    r = client.post(URL, json=_update(47, "ayuda"), headers=_h(OPS_SECRET))
    assert r.status_code == 200
    salida = capsys.readouterr()
    assert TOKEN not in salida.err + salida.out
    assert "fallo procesando mensaje" in salida.err


def test_fallo_inesperado_responde_200_y_no_filtra_el_token(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _boom() -> Any:
        raise RuntimeError(f"no hay bucket para https://api.telegram.org/bot{TOKEN}/x")

    monkeypatch.setattr(main, "publisher_from_env", _boom)
    r = client.post(URL, json=_update(48, "estado"), headers=_h(OPS_SECRET))
    assert r.status_code == 200
    err = capsys.readouterr().err
    assert TOKEN not in err
    assert "fallo en el webhook de ops" in err


@pytest.mark.parametrize(
    "update",
    [{"update_id": "1", "message": {"chat": {"id": ALLOWED}, "text": "hola"}},
     {"update_id": True, "message": {"chat": {"id": ALLOWED}, "text": "hola"}},
     {"update_id": 1, "edited_message": {"chat": {"id": ALLOWED}, "text": "hola"}},
     {"update_id": 1, "message": {"chat": {"id": True}, "text": "hola"}},
     {"update_id": 1, "message": {"chat": {"id": ALLOWED}, "text": "   "}}],
    ids=["update-id-texto", "update-id-bool", "edited", "chat-bool", "texto-vacio"],
)
def test_updates_raros_se_ignoran_sin_error(
    e2e: tuple[TestClient, _FakeTelegram, LocalSnapshotPublisher], update: dict[str, Any]
) -> None:
    client, fake, web = e2e
    assert client.post(URL, json=update, headers=_h(OPS_SECRET)).status_code == 200
    assert fake.calls == []
    assert web.read(ops.INBOX_KEY) is None
