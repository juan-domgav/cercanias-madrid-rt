"""Servicio HTTP: webhook de Telegram y recogida de feedback.

Primera superficie pública del proyecto, así que el webhook se trata como lo que
es: un endpoint que cualquiera en internet puede intentar invocar. Telegram
permite fijar un secreto que viaja en la cabecera
``X-Telegram-Bot-Api-Secret-Token``; sin comprobarlo, cualquiera podría enviar
updates falsos y hacer que el bot conteste cosas que nadie ha preguntado.
"""

from __future__ import annotations

import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request

from cercanias_api.bot import BotContext, BotResult, Feedback, handle_update
from cercanias_domain.departures import LiveTrain, ScheduledStop
from cercanias_domain.station_view import Station
from cercanias_storage.publishers import publisher_from_env

TELEGRAM_API = "https://api.telegram.org"
WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
FEEDBACK_KEY = os.environ.get("FEEDBACK_KEY", "feedback.jsonl")

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


def load_schedule(stop_id: str) -> list[ScheduledStop]:
    data = _cached(f"h:{stop_id}", lambda: _read_json(f"horarios/{stop_id}.json")) or {}
    return [
        ScheduledStop(d["trip"], d["t"], d.get("route", ""), d.get("dest"), bool(d.get("terminates")))
        for d in data.get("departures", [])
    ]


def store_feedback(fb: Feedback) -> None:
    """Añade el aviso a un JSONL en el bucket.

    Se guarda el ``chat_id`` porque sin él no se puede responder a quien reporta,
    pero nada más: ni nombre, ni usuario, ni ubicación. Es el mínimo que hace útil
    el registro.
    """
    publisher = publisher_from_env()
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
    if not WEBHOOK_SECRET or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, WEBHOOK_SECRET
    ):
        raise HTTPException(status_code=403, detail="secreto invalido")

    update = await request.json()
    if not isinstance(update, dict):
        raise HTTPException(status_code=400, detail="update invalido")

    live, prefix = load_live()
    local = datetime.now(tz=ZoneInfo("Europe/Madrid"))
    result = handle_update(update, BotContext(
        stations=load_stations(),
        load_schedule=load_schedule,
        live=live,
        now_minutes=local.hour * 60 + local.minute,
        service_prefix=prefix,
    ))
    if result.feedback:
        store_feedback(result.feedback)
    chat = (update.get("message") or update.get("edited_message") or {}).get("chat") or {}
    if isinstance(chat.get("id"), int):
        send_message(chat["id"], result)
    # Telegram reintenta si no recibe 200: siempre se responde OK aunque el
    # envio haya fallado, para no entrar en un bucle de reintentos.
    return {"ok": True}


#: Cloud Run entrega el trafico al contenedor; escuchar en todas las interfaces
#: dentro del contenedor es lo esperado, no una exposicion adicional.
LISTEN_HOST = "0.0.0.0"


def run() -> None:
    uvicorn.run(app, host=LISTEN_HOST, port=int(os.environ.get("PORT", "8080")))
