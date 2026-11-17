"""Reportes de viajeros al bot de ops (RB-020): privado de cualquiera y menciones en grupos.

Los comandos de ops siguen siendo SOLO del chat de Juan (allowlist). Un viajero
deja un reporte en el bucket PRIVADO (lleva su usuario y chat), nunca en el
buzón público. En un grupo solo cuenta lo que menciona al bot o le responde.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import ops_reports as rep
from cercanias_worker import telegram_ops_poll as bot

TOKEN = "8123456789:SECRETO-NO-LOGUEAR"
BOT_ID = 8123456789
BOT_USERNAME = "CercaniasRTOPS_bot"
JUAN = 5830900654
RIDER = 111222333
GROUP = -1001234567890
NOW = datetime(2026, 11, 17, 9, 40, tzinfo=UTC)


class FakeTelegram:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True, "result": {}})

    def sent(self) -> list[dict[str, Any]]:
        return [b for m, b in self.calls if m == "sendMessage"]


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = FakeTelegram()
        self.web = LocalSnapshotPublisher(tmp_path / "web")
        self.private = LocalSnapshotPublisher(tmp_path / "privado")
        self.client = httpx.Client(transport=httpx.MockTransport(self.fake.handler))
        self.now = NOW

    def send(self, update: dict[str, Any], *, report_chat_ids: set[int] | None = None,
             private: bool = True, raise_on_inbox_error: bool = False) -> None:
        bot.process_update(
            bot.TelegramApi(TOKEN, self.client), self.web, {JUAN}, update,
            now=lambda: self.now,
            raise_on_inbox_error=raise_on_inbox_error,
            reports=rep.ReportConfig(
                store=self.private, bot_id=BOT_ID, bot_username=BOT_USERNAME,
                group_chat_ids=frozenset(report_chat_ids or ()),
            ) if private else None,
        )

    def reports(self) -> list[dict[str, Any]]:
        keys = sorted(self.private.list_keys(rep.REPORTS_PREFIX))
        return [json.loads(self.private.read(k) or b"") for k in keys]


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def _private(update_id: int, text: str | None, *, user: int = RIDER, username: str | None = "viajera",
             **extra: Any) -> dict[str, Any]:
    msg: dict[str, Any] = {"message_id": 500 + update_id, "chat": {"id": user, "type": "private"},
                           "from": {"id": user, "is_bot": False, "username": username}, **extra}
    if text is not None:
        msg["text"] = text
    return {"update_id": update_id, "message": msg}


def _group(update_id: int, text: str | None, *, entities: list[dict[str, Any]] | None = None,
           user: int = RIDER, **extra: Any) -> dict[str, Any]:
    """Mensaje de grupo. ``chat`` y ``chat_type`` en ``extra`` cambian el chat."""
    chat = {"id": extra.pop("chat", GROUP), "type": extra.pop("chat_type", "supergroup"),
            "title": "Cercanías Madrid RT"}
    msg: dict[str, Any] = {
        "message_id": 900 + update_id, "chat": chat,
        "from": {"id": user, "is_bot": False, "username": "viajero"}, **extra,
    }
    if text is not None:
        msg["text"] = text
    if entities is not None:
        msg["entities"] = entities
    return {"update_id": update_id, "message": msg}


def _mention(text: str, word: str = f"@{BOT_USERNAME}") -> list[dict[str, Any]]:
    off = len(text[: text.index(word)].encode("utf-16-le")) // 2
    return [{"type": "mention", "offset": off, "length": len(word.encode("utf-16-le")) // 2}]


# --- 1. Juan: igual que siempre -------------------------------------------------


def test_juan_sigue_teniendo_comandos_y_buzon(env: Env) -> None:
    env.send(_private(1, "estado", user=JUAN))
    assert env.fake.sent()[0]["text"].startswith("Estado Cercanías Madrid")
    env.send(_private(2, "la C4 va rara", user=JUAN))
    assert env.fake.sent()[1]["text"] == bot.ACK_UNKNOWN
    items = json.loads(env.web.read(bot.INBOX_KEY) or b"")["items"]
    assert [i["text"] for i in items] == ["la C4 va rara"]
    assert env.reports() == []


# --- 2. Viajero por privado -----------------------------------------------------


def test_viajero_por_privado_deja_reporte_privado_y_recibe_gracias(env: Env) -> None:
    env.send(_private(10, "En Sol el tablón dice sin datos"))
    assert env.fake.sent() == [{"chat_id": RIDER, "text": rep.ACK_RIDER, "disable_web_page_preview": True,
                                "reply_parameters": {"message_id": 510,
                                                     "allow_sending_without_reply": True}}]
    [r] = env.reports()
    assert r == {
        "ts": NOW.isoformat(), "update_id": 10, "message_id": 510,
        "text": "En Sol el tablón dice sin datos", "source": "private",
        "chat_id": RIDER, "chat_type": "private", "chat_title": None,
        "from_id": RIDER, "from_username": "viajera", "type": "text", "file_id": None,
    }
    # Nunca en el buzón público: lleva usuario y chat.
    assert env.web.read(bot.INBOX_KEY) is None


def test_viajero_no_puede_ejecutar_comandos_de_ops(env: Env) -> None:
    for i, cmd in enumerate(["estado", "/status", "horarios", "regenerar", "/estado@CercaniasRTOPS_bot"]):
        env.send(_private(20 + i, cmd))
    textos = [m["text"] for m in env.fake.sent()]
    assert all(t == rep.ACK_RIDER for t in textos)
    assert not any("Estado Cercanías" in t or "gcloud" in t or "Horarios:" in t for t in textos)
    assert env.reports()[0]["text"] == "estado"


def test_start_del_viajero_recibe_bienvenida_y_no_se_guarda(env: Env) -> None:
    env.send(_private(30, "/start"))
    assert env.fake.sent()[0]["text"] == rep.WELCOME_RIDER
    assert env.reports() == []


def test_foto_del_viajero_se_guarda_con_pie_y_file_id(env: Env) -> None:
    env.send(_private(31, None, caption="Colmenar sin trenes",
                      photo=[{"file_id": "AgACmini"}, {"file_id": "AgACfull"}]))
    [r] = env.reports()
    assert (r["text"], r["type"], r["file_id"]) == ("Colmenar sin trenes", "photo", "AgACfull")
    env.send(_private(32, None, photo=[{"file_id": "AgACsolo"}]))
    assert env.reports()[1]["text"] == "[foto]"


def test_reentrega_no_duplica_ni_vuelve_a_contestar(env: Env) -> None:
    env.send(_private(40, "hola, falla la web"))
    env.send(_private(40, "hola, falla la web"))
    assert len(env.reports()) == 1
    assert len(env.fake.sent()) == 1


def test_mensajes_de_bots_se_ignoran(env: Env) -> None:
    upd = _private(41, "spam")
    upd["message"]["from"]["is_bot"] = True
    env.send(upd)
    assert env.fake.sent() == [] and env.reports() == []


def test_sin_almacen_privado_el_viajero_se_ignora_como_antes(env: Env) -> None:
    env.send(_private(42, "hola"), private=False)
    assert env.fake.sent() == [] and env.reports() == []
    assert env.web.read(bot.INBOX_KEY) is None


def test_mensaje_vacio_se_ignora(env: Env) -> None:
    env.send(_private(43, "   "))
    assert env.fake.sent() == [] and env.reports() == []


# --- 3. Grupos ------------------------------------------------------------------


def test_mencion_en_grupo_deja_reporte_y_contesta_en_el_hilo(env: Env) -> None:
    texto = "@CercaniasRTOPS_bot en Atocha la C5 no aparece"
    env.send(_group(50, texto, entities=_mention(texto)))
    assert env.fake.sent() == [{"chat_id": GROUP, "text": rep.ACK_RIDER, "disable_web_page_preview": True,
                                "reply_parameters": {"message_id": 950,
                                                     "allow_sending_without_reply": True}}]
    [r] = env.reports()
    assert r["source"] == "group" and r["chat_type"] == "supergroup"
    assert r["chat_id"] == GROUP and r["chat_title"] == "Cercanías Madrid RT"
    assert r["from_id"] == RIDER and r["from_username"] == "viajero"
    assert r["text"] == texto and r["message_id"] == 950


def test_mencion_con_emoji_antes_usa_offsets_utf16(env: Env) -> None:
    texto = "🚆🚆 ojo @cercaniasrtops_bot la web no carga"
    env.send(_group(51, texto, entities=_mention(texto, "@cercaniasrtops_bot")))
    assert len(env.reports()) == 1


def test_respuesta_a_un_mensaje_del_bot_cuenta(env: Env) -> None:
    env.send(_group(52, "sigue fallando", reply_to_message={
        "message_id": 3, "from": {"id": BOT_ID, "is_bot": True, "username": BOT_USERNAME}}))
    assert len(env.reports()) == 1


def test_foto_con_mencion_en_el_pie(env: Env) -> None:
    pie = "@CercaniasRTOPS_bot mirad esto"
    env.send(_group(53, None, caption=pie, caption_entities=_mention(pie),
                    photo=[{"file_id": "AgACg"}]))
    [r] = env.reports()
    assert (r["type"], r["file_id"], r["text"]) == ("photo", "AgACg", pie)


def test_en_un_hilo_de_tema_contesta_en_el_mismo_tema(env: Env) -> None:
    texto = "@CercaniasRTOPS_bot hola"
    env.send(_group(54, texto, entities=_mention(texto), is_topic_message=True, message_thread_id=77))
    assert env.fake.sent()[0]["message_thread_id"] == 77


@pytest.mark.parametrize(
    "update",
    [
        _group(60, "buenos días a todos"),
        _group(61, "hablando de @otro_bot", entities=_mention("hablando de @otro_bot", "@otro_bot")),
        _group(62, "respuesta a otra persona", reply_to_message={"message_id": 4, "from": {"id": 5}}),
        _group(63, None, new_chat_members=[{"id": BOT_ID}]),
        _group(64, "/estado@CercaniasRTOPS_bot",
               entities=[{"type": "bot_command", "offset": 0, "length": 26}]),
    ],
    ids=["charla", "otra-mencion", "respuesta-a-otro", "servicio", "comando"],
)
def test_en_grupo_lo_que_no_va_al_bot_se_ignora_en_silencio(env: Env, update: dict[str, Any]) -> None:
    env.send(update)
    assert env.fake.calls == [] and env.reports() == []


def test_juan_en_el_grupo_no_ejecuta_comandos(env: Env) -> None:
    texto = "@CercaniasRTOPS_bot estado"
    env.send(_group(65, texto, entities=_mention(texto), user=JUAN))
    assert env.fake.sent()[0]["text"] == rep.ACK_RIDER
    assert env.reports()[0]["from_id"] == JUAN
    assert env.web.read(bot.INBOX_KEY) is None


def test_un_grupo_con_el_id_de_juan_en_la_allowlist_no_ejecuta_comandos(env: Env) -> None:
    """Aunque alguien meta un id de grupo en OPS_ALLOWED_CHAT_IDS, desde un grupo
    no hay comandos: la allowlist solo vale en chats privados."""
    texto = "@CercaniasRTOPS_bot estado"
    upd = _group(66, texto, entities=_mention(texto), chat=JUAN, chat_type="group")
    env.send(upd)
    assert env.fake.sent()[0]["text"] == rep.ACK_RIDER


def test_lista_de_grupos_permitidos(env: Env) -> None:
    texto = "@CercaniasRTOPS_bot hola"
    env.send(_group(67, texto, entities=_mention(texto), chat=-100999), report_chat_ids={GROUP})
    assert env.fake.calls == [] and env.reports() == []
    env.send(_group(68, texto, entities=_mention(texto)), report_chat_ids={GROUP})
    assert len(env.reports()) == 1


def test_canales_se_ignoran(env: Env) -> None:
    env.send(_group(69, "@CercaniasRTOPS_bot x", entities=_mention("@CercaniasRTOPS_bot x"),
                    chat_type="channel"))
    assert env.fake.calls == [] and env.reports() == []


# --- 4. Límite por usuario ------------------------------------------------------


def test_limite_por_usuario_avisa_una_vez_y_luego_calla(env: Env) -> None:
    for i in range(rep.RATE_MAX_REPORTS + 3):
        env.send(_private(100 + i, f"mensaje {i}"))
    assert len(env.reports()) == rep.RATE_MAX_REPORTS
    textos = [m["text"] for m in env.fake.sent()]
    assert textos.count(rep.ACK_RIDER) == rep.RATE_MAX_REPORTS
    assert textos.count(rep.RATE_LIMITED_RIDER) == 1
    # Pasada la ventana vuelve a aceptar.
    env.now = NOW + timedelta(seconds=rep.RATE_WINDOW_S + 1)
    env.send(_private(200, "otra vez"))
    assert len(env.reports()) == rep.RATE_MAX_REPORTS + 1


def test_el_limite_es_por_usuario(env: Env) -> None:
    for i in range(rep.RATE_MAX_REPORTS):
        env.send(_private(300 + i, f"a{i}"))
    env.send(_private(400, "b", user=RIDER + 1))
    assert len(env.reports()) == rep.RATE_MAX_REPORTS + 1


# --- 5. Fallos ------------------------------------------------------------------


class _Roto(LocalSnapshotPublisher):
    def publish(self, key: str, payload: bytes, **kw: Any) -> str:
        if key.startswith(rep.REPORTS_PREFIX):
            raise RuntimeError("GCS caído")
        return super().publish(key, payload, **kw)


def test_fallo_guardando_el_reporte_lanza_en_webhook_y_no_contesta(tmp_path: Path) -> None:
    env = Env(tmp_path)
    env.private = _Roto(tmp_path / "privado")
    with pytest.raises(bot.InboxWriteError):
        env.send(_private(500, "hola"), raise_on_inbox_error=True)
    assert env.fake.sent() == []
    env.send(_private(501, "hola"))  # por defecto no lanza
    assert env.fake.sent() == []


def test_el_log_no_lleva_token_ni_usuario(env: Env, capsys: pytest.CaptureFixture[str]) -> None:
    env.send(_private(600, "hola"))
    err = capsys.readouterr().err
    assert "reporte de viajero guardado" in err
    assert TOKEN not in err and "viajera" not in err


def test_bot_id_desde_el_token() -> None:
    assert rep.bot_id_from_token(TOKEN) == BOT_ID
    assert rep.bot_id_from_token("") is None
    assert rep.bot_id_from_token("abc:def") is None
