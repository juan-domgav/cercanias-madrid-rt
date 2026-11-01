"""Servicio HTTP: webhook de Telegram y recogida de feedback.

Primera superficie pública del proyecto, así que el webhook se trata como lo que
es: un endpoint que cualquiera en internet puede intentar invocar. Telegram
permite fijar un secreto que viaja en la cabecera
``X-Telegram-Bot-Api-Secret-Token``; sin comprobarlo, cualquiera podría enviar
updates falsos y hacer que el bot conteste cosas que nadie ha preguntado.

El bot de operaciones (``@CercaniasRTOPS_bot``) entra por ``/telegram/ops-webhook``
con su PROPIO secreto (``TELEGRAM_OPS_WEBHOOK_SECRET``) y su propio token
(``TELEGRAM_OPS_BOT_TOKEN``). La lógica es la del job ``telegram-ops-poll``
(:func:`cercanias_worker.telegram_ops_poll.process_update`): misma allowlist,
mismos comandos de solo lectura y mismo buzón ``ops/inbox.json``. Ver RB-005.
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from cercanias_api.bot import BotContext, BotResult, Feedback, handle_update
from cercanias_domain.departures import LiveTrain
from cercanias_domain.station_view import Station
from cercanias_storage.publishers import GcsSnapshotPublisher, publisher_from_env
from cercanias_worker import telegram_ops_poll as ops

TELEGRAM_API = "https://api.telegram.org"
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FEEDBACK_KEY = os.environ.get("FEEDBACK_KEY", "feedback.jsonl")
#: Bucket PRIVADO. El feedback lleva un chat_id, que es un dato personal, y el
#: bucket de la web es publico por definicion: cualquiera podia leerlo en una URL
#: adivinable. Se separan por eso, no por orden.
PRIVATE_BUCKET = os.environ.get("PRIVATE_BUCKET", "")

#: Bot de operaciones. Secreto DISTINTO del bot público: filtrar uno no abre el
#: otro. Sin secreto configurado el endpoint rechaza todo (403).
#: ``strip``: un salto de línea al crear el secreto no debe dejar el bot en 403
#: (Telegram solo admite ``A-Za-z0-9_-`` en ``secret_token``).
OPS_WEBHOOK_SECRET = os.environ.get("TELEGRAM_OPS_WEBHOOK_SECRET", "").strip()
OPS_BOT_TOKEN = os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "").strip()
#: Misma allowlist que el job (``ALLOWED_CHAT_IDS`` allí). Nombre propio aquí
#: para que nadie crea que restringe el bot público. Vacía = nadie (cierra).
OPS_ALLOWED_CHAT_IDS = ops.parse_allowed_chat_ids(
    os.environ.get("OPS_ALLOWED_CHAT_IDS", ops.DEFAULT_ALLOWED_CHAT_IDS)
)

#: Los datos cambian cada minuto; cachearlos unos segundos evita releer el bucket
#: en cada mensaje sin llegar a servir nada rancio.
CACHE_SECONDS = 20

app = FastAPI(title="Cercanías Madrid API", docs_url=None, redoc_url=None)
_cache: dict[str, tuple[float, Any]] = {}


def _now() -> float:
    return datetime.now(tz=UTC).timestamp()


def _cached(key: str, loader: Any) -> Any:
    hit = _cache.get(key)
    if hit and _now() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = loader()
    _cache[key] = (_now(), value)
    return value


def _read_json(key: str) -> Any:
    raw = publisher_from_env().read(key)
    return json.loads(raw) if raw else None


def load_stations() -> list[Station]:
    data = _cached("estaciones", lambda: _read_json("estaciones.json")) or {}
    return [
        Station(s["renfe_stop_id"], s["name"], s["latitude"], s["longitude"])
        for s in data.get("stations", [])
        if s.get("latitude") and s.get("longitude")
    ]


def load_live() -> tuple[dict[str, LiveTrain], str]:
    snap = _cached("snapshot", lambda: _read_json("snapshot.json")) or {}
    vehicles = snap.get("vehicles", [])
    live = {
        v["trip_id"]: LiveTrain(v["trip_id"], v.get("delay_seconds"), v.get("platform"))
        for v in vehicles
        if v.get("trip_id")
    }
    # El indice de calendario avanza a diario (ADR-012): se deduce del prefijo
    # mas frecuente entre los trenes en circulacion, no se fija en configuracion.
    counts: dict[str, int] = {}
    for v in vehicles:
        p = (v.get("trip_id") or "")[:5]
        if p:
            counts[p] = counts.get(p, 0) + 1
    prefix = max(counts, key=lambda k: counts[k]) if counts else ""
    return live, prefix


def load_boards() -> dict[str, Any]:
    """Tablones ya calculados por la ingesta.

    El bot NO recalcula: consume el mismo ``boards.json`` que pinta la web. Antes
    leia ``horarios/{stop_id}.json`` y dejo de funcionar en silencio cuando esos
    ficheros se retiraron — el bot dejo de mostrar trenes sin un solo error.
    """
    return _cached("boards", lambda: _read_json("boards.json")) or {}


def _private_publisher() -> Any:
    """Publicador contra el bucket privado."""
    if PRIVATE_BUCKET:
        return GcsSnapshotPublisher(PRIVATE_BUCKET)
    return publisher_from_env()


def store_feedback(fb: Feedback) -> None:
    """Añade el aviso a un JSONL en el bucket PRIVADO.

    Se guarda el ``chat_id`` porque sin él no se puede responder a quien reporta,
    pero nada más: ni nombre, ni usuario, ni ubicación. Y nunca en el bucket
    publico: es un identificador personal.
    """
    publisher = _private_publisher()
    previo = publisher.read(FEEDBACK_KEY) or b""
    entrada = json.dumps(
        {"at": datetime.now(tz=UTC).isoformat(), "chat_id": fb.chat_id, "text": fb.text},
        ensure_ascii=False,
    ).encode("utf-8")
    publisher.publish(FEEDBACK_KEY, previo + entrada + b"\n", content_type="application/x-ndjson")


def send_message(chat_id: int, result: BotResult) -> None:
    if not result.reply or not BOT_TOKEN:
        return
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": result.reply.text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if result.reply.reply_markup:
        payload["reply_markup"] = result.reply.reply_markup
    with httpx.Client(timeout=15.0) as client:
        client.post(f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage", json=payload)


def _secret_ok(received: str, expected: str) -> bool:
    """Compara el secreto de Telegram en tiempo constante; sin secreto, rechaza.

    Se comparan bytes: ``hmac.compare_digest`` con ``str`` lanza ``TypeError``
    si hay caracteres no ASCII, y una cabecera así acabaría en un 500.
    """
    if not expected:
        return False
    return hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))


@app.get("/health")
def health() -> dict[str, Any]:
    live, prefix = load_live()
    return {"ok": True, "trains": len(live), "service": prefix}


@app.post("/telegram/webhook")
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict[str, bool]:
    # compare_digest: la comparacion normal filtra informacion por tiempo.
    if not _secret_ok(x_telegram_bot_api_secret_token, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="secreto invalido")

    update = await request.json()
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="update invalido")

    result = handle_update(update, BotContext(
        stations=load_stations(),
        boards=load_boards(),
    ))
    if result.feedback:
        store_feedback(result.feedback)
    chat = (update.get("message") or update.get("edited_message") or {}).get("chat") or {}
    if isinstance(chat.get("id"), int):
        send_message(chat["id"], result)
    # Telegram reintenta si no recibe 200: siempre se responde OK aunque el
    # envio haya fallado, para no entrar en un bucle de reintentos.
    return {"ok": True}


def handle_ops_update(update: dict[str, Any]) -> bool:
    """Atiende un update del bot de ops con la lógica del job de sondeo.

    Devuelve ``False`` si el update NO quedó guardado y conviene que Telegram lo
    reentregue (no hay almacén, o falló la escritura del buzón antes de
    contestar). Cualquier otro fallo (p. ej. al responder) se registra y
    devuelve ``True``: un reintento duplicaría respuestas. No hay offset: con
    webhook, Telegram da el update por entregado al recibir el 200.
    """
    try:
        web = publisher_from_env()
    except Exception as exc:
        ops.log("ERROR", "fallo en el webhook de ops", error=ops.redact(repr(exc), OPS_BOT_TOKEN))
        return False
    try:
        with httpx.Client() as client:
            ops.process_update(
                ops.TelegramApi(OPS_BOT_TOKEN, client),
                web,
                OPS_ALLOWED_CHAT_IDS,
                update,
                raise_on_inbox_error=True,
            )
    except ops.InboxWriteError:
        return False
    except Exception as exc:
        ops.log("ERROR", "fallo en el webhook de ops", error=ops.redact(repr(exc), OPS_BOT_TOKEN))
    return True


@app.post("/telegram/ops-webhook")
async def telegram_ops_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str = Header(default=""),
) -> dict[str, bool]:
    if not _secret_ok(x_telegram_bot_api_secret_token, OPS_WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="secreto invalido")
    try:
        update = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="update invalido") from None
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="update invalido")
    if not OPS_BOT_TOKEN:
        # Sin token no se puede contestar. 503: Telegram guarda el update y lo
        # reintenta (hasta 24 h), y getWebhookInfo muestra el error; con un 200
        # el mensaje se perdería sin que nadie lo viera.
        ops.log("ERROR", "falta TELEGRAM_OPS_BOT_TOKEN en cercanias-api")
        raise HTTPException(status_code=503, detail="bot de ops sin configurar")
    # E/S bloqueante (GCS, Telegram): fuera del bucle de eventos.
    if await run_in_threadpool(handle_ops_update, update) is False:
        # Sin guardar en el buzón: que Telegram lo reentregue (dedupe por update_id).
        raise HTTPException(status_code=503, detail="reintentar")
    return {"ok": True}


#: Cloud Run entrega el trafico al contenedor; escuchar en todas las interfaces
#: dentro del contenedor es lo esperado, no una exposicion adicional.
LISTEN_HOST = "0.0.0.0"


def run() -> None:
    uvicorn.run(app, host=LISTEN_HOST, port=int(os.environ.get("PORT", "8080")))
