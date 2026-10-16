"""Bot de operaciones por Telegram: una pasada de ``getUpdates`` por invocación.

Lo lanza Cloud Scheduler cada minuto como el job ``cercanias-telegram-ops``
(misma imagen que la ingesta). Es deliberadamente tonto: comandos con guion,
sin IA y sin ejecutar nada destructivo. Ver RB-005.

- Token: ``TELEGRAM_OPS_BOT_TOKEN`` (secreto ``telegram-ops-bot-token``). Nunca
  se registra: los errores se limpian antes de loguearse.
- Solo responde a los chats de ``ALLOWED_CHAT_IDS``; el resto se ignora y solo
  avanza el offset.
- El offset vive en el bucket PRIVADO (``ops/telegram-offset.json``).
- Los mensajes no reconocidos van a ``ops/inbox.json`` del bucket WEB, que es
  PÚBLICO: Wall-E lo lee sin credenciales. Solo se guardan ``ts``, ``update_id``
  y ``text`` (ni chat_id ni nombre), pero el texto queda legible por cualquiera.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from cercanias_storage.publishers import GcsSnapshotPublisher
from cercanias_worker.cloud_tick import _next_service_id, _schedule_days_left

COMPONENT = "telegram-ops"
OFFSET_KEY = "ops/telegram-offset.json"
INBOX_KEY = "ops/inbox.json"
INBOX_MAX_ITEMS = 20
DEFAULT_ALLOWED_CHAT_IDS = "5830900654"
SCHEDULES_PREFIX = "horarios"
ACK_UNKNOWN = "Recibido, lo paso a Cercanías Ops"

RUNBOOK_REGENERAR = (
    "Regenerar horarios (RB-003). No lo ejecuto yo; lánzalo tú:\n"
    "gcloud run jobs execute cercanias-horarios --region europe-southwest1 "
    "--project cercanias-en-hora --wait"
)

AYUDA = (
    "Comandos:\n"
    "• estado / status — salud de boards.json y snapshot.json\n"
    "• horarios — días de horario que quedan y último fichero\n"
    "• regenerar — comando del runbook para regenerar horarios (no lo ejecuta)\n"
    "• ayuda / help — esta lista\n"
    "Cualquier otro mensaje se pasa a Cercanías Ops."
)

COMMANDS: dict[str, str] = {
    "estado": "estado",
    "status": "estado",
    "horarios": "horarios",
    "schedules": "horarios",
    "regenerar": "regenerar",
    "regenerate": "regenerar",
    "ayuda": "ayuda",
    "help": "ayuda",
    "start": "ayuda",
}


class Store(Protocol):
    def read(self, key: str) -> bytes | None: ...

    def publish(self, key: str, payload: bytes, *, content_type: str, cache_control: str = ...) -> str: ...


def log(severity: str, message: str, **fields: Any) -> None:
    entry = {"severity": severity, "message": message, "component": COMPONENT, **fields}
    print(json.dumps(entry, ensure_ascii=False, default=str), file=sys.stderr, flush=True)


def redact(text: str, token: str) -> str:
    """Quita el token de cualquier texto (las URLs de la API lo llevan dentro)."""
    return text.replace(token, "<token>") if token else text


def parse_allowed_chat_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        limpio = part.strip()
        if limpio.lstrip("-").isdigit():
            ids.add(int(limpio))
    return ids


def parse_command(text: str) -> str | None:
    """Comando canónico o ``None``. Acepta ``/estado``, ``Estado``, ``/status@bot``."""
    words = text.strip().split()
    if not words:
        return None
    first = words[0].lower().lstrip("/").split("@", 1)[0]
    return COMMANDS.get(first)


class TelegramApi:
    def __init__(self, token: str, client: httpx.Client) -> None:
        self._token = token
        self._client = client

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        try:
            response = self._client.post(url, json=payload, timeout=20)
            data = response.json()
        except Exception as exc:
            raise RuntimeError(redact(f"{method}: {type(exc).__name__}: {exc}", self._token)) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = data.get("description") if isinstance(data, dict) else None
            raise RuntimeError(redact(f"{method}: HTTP {response.status_code} {desc}", self._token))
        return data.get("result")

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": 0, "allowed_updates": ["message"]}
        if offset is not None:
            payload["offset"] = offset
        result = self._call("getUpdates", payload)
        return [u for u in result or [] if isinstance(u, dict)]

    def send_message(self, chat_id: int, text: str) -> None:
        self._call("sendMessage", {"chat_id": chat_id, "text": text, "disable_web_page_preview": True})


def _read_json(store: Store, key: str) -> Any:
    raw = store.read(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        log("WARNING", f"{key} ilegible", key=key)
        return None


def load_offset(store: Store) -> int | None:
    data = _read_json(store, OFFSET_KEY)
    if isinstance(data, dict) and isinstance(data.get("offset"), int):
        offset: int = data["offset"]
        return offset
    return None


def save_offset(store: Store, offset: int) -> None:
    payload = {"offset": offset, "updated_at": datetime.now(tz=UTC).isoformat()}
    store.publish(
        OFFSET_KEY,
        json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        cache_control="no-store",
    )


def append_inbox(store: Store, item: dict[str, Any]) -> None:
    """Añade al buzón público, conservando los últimos ``INBOX_MAX_ITEMS``."""
    data = _read_json(store, INBOX_KEY)
    items = data.get("items") if isinstance(data, dict) else None
    lista = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    if any(i.get("update_id") == item["update_id"] for i in lista):
        return
    lista.append({"ts": item["ts"], "update_id": item["update_id"], "text": item["text"]})
    payload = {"items": lista[-INBOX_MAX_ITEMS:]}
    store.publish(
        INBOX_KEY,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
        cache_control="no-store",
    )


def _age_seconds(marca: Any, now: datetime) -> int | None:
    if not isinstance(marca, str):
        return None
    try:
        return int((now - datetime.fromisoformat(marca)).total_seconds())
    except (ValueError, TypeError):
        return None


def build_status(web: Store, now: datetime) -> str:
    boards = _read_json(web, "boards.json")
    snapshot = _read_json(web, "snapshot.json")
    lineas: list[str] = ["Estado Cercanías Madrid"]
    if isinstance(boards, dict):
        stations = boards.get("stations") or {}
        salidas = sum(len(s.get("departures") or []) for s in stations.values() if isinstance(s, dict))
        health = boards.get("health") or {}
        lineas += [
            f"boards.generated_at: {boards.get('generated_at')} "
            f"(hace {_age_seconds(boards.get('generated_at'), now)} s)",
            f"servicio: {boards.get('service')}",
            f"estaciones: {len(stations)} (con salidas: {boards.get('stations_with_departures')})",
            f"salidas: {salidas}",
            "health: " + ", ".join(f"{k}={v}" for k, v in health.items()),
        ]
    else:
        lineas.append("boards.json: NO DISPONIBLE")
    if isinstance(snapshot, dict):
        lineas += [
            f"snapshot.generated_at: {snapshot.get('generated_at')}",
            f"edad snapshot: {_age_seconds(snapshot.get('generated_at'), now)} s",
            f"trenes: {len(snapshot.get('vehicles') or [])}",
            f"degraded: {bool(snapshot.get('degraded'))}"
            + (f" ({snapshot.get('reason')})" if snapshot.get("reason") else ""),
        ]
    else:
        lineas.append("snapshot.json: NO DISPONIBLE")
    return "\n".join(lineas)


def build_horarios(web: Any) -> str:
    boards = _read_json(web, "boards.json")
    service = boards.get("service") if isinstance(boards, dict) else None
    if not isinstance(service, str) or not service:
        return "Horarios: no hay servicio en boards.json"
    dias = _schedule_days_left(web, service)
    ultimo = service
    if dias:
        for _ in range(dias):
            ultimo = _next_service_id(ultimo) or ultimo
    elif dias is None:
        ultimo = "?"
    return (
        f"Horarios: servicio {service}\n"
        f"días por delante: {dias if dias is not None else 'desconocido'}\n"
        f"último fichero: {SCHEDULES_PREFIX}/{ultimo}.json"
    )


def reply_for(command: str, web: Any, now: datetime) -> str:
    if command == "estado":
        return build_status(web, now)
    if command == "horarios":
        return build_horarios(web)
    if command == "regenerar":
        return RUNBOOK_REGENERAR
    return AYUDA


def poll_once(
    api: TelegramApi,
    private: Store,
    web: Any,
    allowed: set[int],
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> int:
    """Procesa los updates pendientes. Devuelve cuántos se procesaron."""
    offset = load_offset(private)
    updates = api.get_updates(offset)
    if not updates:
        log("INFO", "sin mensajes", offset=offset)
        return 0
    ultimo = offset
    for update in sorted(updates, key=lambda u: int(u.get("update_id", 0))):
        update_id = int(update.get("update_id", 0))
        ultimo = update_id + 1 if ultimo is None else max(ultimo, update_id + 1)
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = message.get("text")
        if chat_id not in allowed:
            log("WARNING", "chat no permitido, se ignora", update_id=update_id, chat_id=chat_id)
            continue
        if not isinstance(text, str) or not text.strip():
            continue
        command = parse_command(text)
        try:
            if command is None:
                append_inbox(web, {"ts": now().isoformat(), "update_id": update_id, "text": text})
                api.send_message(chat_id, ACK_UNKNOWN)
            else:
                api.send_message(chat_id, reply_for(command, web, now()))
            log("INFO", "mensaje procesado", update_id=update_id, command=command or "inbox")
        except Exception as exc:
            log("ERROR", "fallo procesando mensaje", update_id=update_id, error=str(exc))
    if ultimo is not None and ultimo != offset:
        save_offset(private, ultimo)
    return len(updates)


def main() -> int:
    token = os.environ.get("TELEGRAM_OPS_BOT_TOKEN", "").strip()
    if not token:
        log("ERROR", "falta TELEGRAM_OPS_BOT_TOKEN")
        return 2
    allowed = parse_allowed_chat_ids(os.environ.get("ALLOWED_CHAT_IDS", DEFAULT_ALLOWED_CHAT_IDS))
    web = GcsSnapshotPublisher(os.environ.get("SNAPSHOT_BUCKET_GCS", "cercanias-madrid-rt-web"))
    private = GcsSnapshotPublisher(os.environ.get("PRIVATE_BUCKET", "cercanias-madrid-rt-privado"))
    try:
        with httpx.Client() as client:
            n = poll_once(TelegramApi(token, client), private, web, allowed)
    except Exception as exc:
        log("ERROR", "fallo en la pasada", error=redact(str(exc), token))
        return 1
    log("INFO", "pasada terminada", updates=n)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
