"""Bot de operaciones por Telegram: una pasada de ``getUpdates`` por invocación.

Lo lanza Cloud Scheduler cada minuto como el job ``cercanias-telegram-ops``
(misma imagen que la ingesta). Es deliberadamente tonto: comandos con guion,
sin IA y sin ejecutar nada destructivo. Ver RB-005.

La misma lógica por update (:func:`process_update`) la usa también el webhook
``/telegram/ops-webhook`` de ``cercanias-api``. Este job se conserva para poder
volver al sondeo (rollback) sin desplegar nada: ``deleteWebhook`` y reanudar
el planificador.

- Token: ``TELEGRAM_OPS_BOT_TOKEN`` (secreto ``telegram-ops-bot-token``). Nunca
  se registra: los errores se limpian antes de loguearse.
- Solo responde a los chats de ``ALLOWED_CHAT_IDS``; el resto se ignora y solo
  avanza el offset.
- El offset vive en el bucket PRIVADO (``ops/telegram-offset.json``).
- Los mensajes no reconocidos van a ``ops/inbox.json`` del bucket WEB, que es
  PÚBLICO: Wall-E lo lee sin credenciales. Solo se guardan ``ts``, ``update_id``
  y ``text`` (ni chat_id ni nombre), pero el texto queda legible por cualquiera.
- Fotos y documentos (capturas) también van al buzón: ``text`` es el pie de foto
  o un marcador (``[foto]``), y se añaden ``type`` y ``file_id`` (el de mayor
  resolución). Antes se descartaban en silencio con un 200. Un ``file_id`` no
  sirve sin el token del bot. Los items de texto no cambian de forma.
- Viajeros (solo webhook, con bucket privado): ver :mod:`cercanias_worker.ops_reports`
  (RB-020). Sus mensajes nunca van al buzón público ni ejecutan comandos.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

from cercanias_storage.publishers import GcsSnapshotPublisher, PreconditionFailedError
from cercanias_worker import ops_reports as rep
from cercanias_worker.cloud_tick import _next_service_id, _schedule_days_left
from cercanias_worker.ops_reports import ATTACHMENT_PLACEHOLDERS

COMPONENT = "telegram-ops"
OFFSET_KEY = "ops/telegram-offset.json"
INBOX_KEY = "ops/inbox.json"
INBOX_MAX_ITEMS = 20
INBOX_MAX_CHARS = 1000
HTTP_TIMEOUT_S = 10.0
#: Presupuesto de la pasada: el job tiene --task-timeout 60s; lo que no quepa
#: se queda para el minuto siguiente (el offset ya se guarda por update).
RUN_BUDGET_S = 35.0
DEFAULT_ALLOWED_CHAT_IDS = "5830900654"
SCHEDULES_PREFIX = "horarios"
ACK_UNKNOWN = "Recibido, lo paso a Cercanías Ops"
#: Claves opcionales de un item del buzón (además de ts, update_id y text).
INBOX_OPTIONAL_KEYS = ("type", "file_id")
#: Reintentos de la escritura condicional del buzón si otro escribe a la vez.
INBOX_CAS_ATTEMPTS = 5

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

    def publish(
        self, key: str, payload: bytes, *, content_type: str, cache_control: str = ..., gzip: bool = ...
    ) -> str: ...


@runtime_checkable
class VersionedStore(Protocol):
    """Almacén con escritura condicional (GCS y disco; S3 no)."""

    def read_versioned(self, key: str) -> tuple[bytes | None, int]: ...

    def publish(  # noqa: PLR0913 - misma firma que los publicadores
        self,
        key: str,
        payload: bytes,
        *,
        content_type: str,
        cache_control: str = ...,
        gzip: bool = ...,
        if_generation_match: int | None = ...,
    ) -> str: ...


class InboxWriteError(RuntimeError):
    """No se pudo guardar un mensaje en el buzón (el webhook pide reintento)."""


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
            response = self._client.post(url, json=payload, timeout=HTTP_TIMEOUT_S)
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

    def send_message(
        self, chat_id: int, text: str, *, reply_to: int | None = None, thread_id: int | None = None
    ) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if reply_to is not None:
            # Contestar EN el hilo: en un grupo, que se vea a qué mensaje responde.
            payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        self._call("sendMessage", payload)


def _read_json(store: Store, key: str) -> Any:
    return _parse_json(store.read(key), key)


def _parse_json(raw: bytes | None, key: str) -> Any:
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


def _inbox_with(raw: bytes | None, item: dict[str, Any]) -> bytes | None:
    """Buzón con ``item`` añadido, o ``None`` si ya estaba (dedupe por ``update_id``)."""
    data = _parse_json(raw, INBOX_KEY)
    items = data.get("items") if isinstance(data, dict) else None
    lista = [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    if any(i.get("update_id") == item["update_id"] for i in lista):
        return None
    nuevo = {"ts": item["ts"], "update_id": item["update_id"], "text": str(item["text"])[:INBOX_MAX_CHARS]}
    nuevo.update({k: str(item[k]) for k in INBOX_OPTIONAL_KEYS if item.get(k)})
    lista.append(nuevo)
    return json.dumps({"items": lista[-INBOX_MAX_ITEMS:]}, ensure_ascii=False).encode("utf-8")


def append_inbox(store: Store, item: dict[str, Any]) -> None:
    """Añade al buzón público, conservando los últimos ``INBOX_MAX_ITEMS``.

    Con un almacén versionado (GCS, disco) la escritura es condicional a la
    generación leída: si otro update escribió entre medias (dos entregas del
    webhook a la vez, o el job de sondeo solapado durante el cambio), se relee
    y se reintenta en vez de pisar su mensaje.
    """
    if not isinstance(store, VersionedStore):
        payload = _inbox_with(store.read(INBOX_KEY), item)
        if payload is not None:
            store.publish(
                INBOX_KEY, payload, content_type="application/json", cache_control="no-store", gzip=True
            )
        return
    for intento in range(1, INBOX_CAS_ATTEMPTS + 1):
        try:
            raw, generation = store.read_versioned(INBOX_KEY)
            payload = _inbox_with(raw, item)
            if payload is None:
                return
            store.publish(
                INBOX_KEY,
                payload,
                content_type="application/json",
                cache_control="no-store",
                gzip=True,
                if_generation_match=generation,
            )
        except PreconditionFailedError:
            log("WARNING", "buzón modificado a la vez, se reintenta", intento=intento)
            continue
        return
    raise PreconditionFailedError(f"{INBOX_KEY}: conflicto tras {INBOX_CAS_ATTEMPTS} intentos")


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


def poll_once(  # noqa: PLR0913 - dependencias inyectables para los tests
    api: TelegramApi,
    private: Store,
    web: Any,
    allowed: set[int],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    agotado: Callable[[], bool] | None = None,
) -> int:
    """Procesa los updates pendientes. Devuelve cuántos se procesaron.

    Semántica "como mucho una vez": el offset se guarda ANTES de responder a
    cada update, así un fallo o un timeout a mitad de lote nunca repite
    respuestas (a cambio, un mensaje puede quedarse sin contestar).
    """
    if agotado is None:
        limite = time.monotonic() + RUN_BUDGET_S
        agotado = lambda: time.monotonic() > limite  # noqa: E731
    offset = load_offset(private)
    updates = api.get_updates(offset)
    if not updates:
        log("INFO", "sin mensajes", offset=offset)
        return 0
    validos = [u for u in updates if isinstance(u.get("update_id"), int)]
    procesados = 0
    for update in sorted(validos, key=lambda u: int(u["update_id"])):
        if agotado():
            log("WARNING", "presupuesto agotado, el resto queda para la siguiente pasada")
            break
        update_id = int(update["update_id"])
        if offset is not None and update_id < offset:
            continue
        offset = update_id + 1
        save_offset(private, offset)
        procesados += 1
        process_update(api, web, allowed, update, now=now)
    return procesados


def process_update(  # noqa: PLR0913 - dependencias inyectables y opciones con nombre
    api: TelegramApi,
    web: Any,
    allowed: set[int],
    update: dict[str, Any],
    *,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    raise_on_inbox_error: bool = False,
    reports: rep.ReportConfig | None = None,
) -> None:
    """Atiende UN update: allowlist, comando o buzón, y respuesta.

    Con ``reports`` (webhook de ``cercanias-api`` con bucket privado) también se
    aceptan reportes de viajeros: por privado de cualquiera y, en grupos, solo
    menciones o respuestas al bot (ver :mod:`cercanias_worker.ops_reports`,
    RB-020). Sin ``reports`` (job de sondeo) un chat ajeno se ignora como antes.

    Compartido por el sondeo (:func:`poll_once`) y el webhook de
    ``cercanias-api``. Por defecto nunca lanza: un fallo se registra y el update
    se da por atendido (como mucho una vez, la semántica del sondeo). Con
    ``raise_on_inbox_error`` (webhook), si el mensaje NO se pudo guardar en el
    buzón se lanza :class:`InboxWriteError` antes de contestar, para que la API
    responda 5xx y Telegram lo reentregue (el dedupe por ``update_id`` evita
    duplicados). Solo lee el bucket, escribe el buzón y contesta; no hay comando
    que despliegue, mergee ni borre nada.
    """
    update_id = update.get("update_id")
    if not isinstance(update_id, int) or isinstance(update_id, bool):
        return
    message = update.get("message")
    if not isinstance(message, dict):
        return
    chat = message.get("chat")
    chat_id = chat.get("id") if isinstance(chat, dict) else None
    chat_type = chat.get("type") if isinstance(chat, dict) else None
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        log("WARNING", "chat no permitido, se ignora", update_id=update_id, chat_id=chat_id)
        return
    entrada = _Incoming(update_id, message, chat_id, chat_type, now())
    # La allowlist (comandos de ops y buzón público) solo vale en chats privados:
    # desde un grupo nunca se ejecuta nada, aunque su id estuviera en la lista.
    if chat_id in allowed and chat_type in (None, "private"):
        _handle_owner(api, web, entrada, raise_on_error=raise_on_inbox_error)
    elif reports is not None and chat_type in ("private", "group", "supergroup"):
        _handle_report(api, reports, entrada, raise_on_error=raise_on_inbox_error)
    else:
        log("WARNING", "chat no permitido, se ignora", update_id=update_id, chat_id=chat_id)


@dataclass(frozen=True)
class _Incoming:
    """Un mensaje ya validado: update, chat y momento de llegada."""

    update_id: int
    message: dict[str, Any]
    chat_id: int
    chat_type: str | None
    ahora: datetime

    @property
    def in_group(self) -> bool:
        return self.chat_type in ("group", "supergroup")


def _handle_owner(api: TelegramApi, web: Any, entrada: _Incoming, *, raise_on_error: bool) -> None:
    """Chat de Juan: comandos de ops o buzón público (comportamiento de siempre)."""
    update_id, message, chat_id = entrada.update_id, entrada.message, entrada.chat_id
    text = message.get("text")
    adjunto = None if isinstance(text, str) and text.strip() else rep.attachment(message)
    item: dict[str, Any] = {"ts": entrada.ahora.isoformat(), "update_id": update_id, "text": text}
    if adjunto is not None:
        # Captura con o sin pie: siempre al buzón (un pie «estado» es un reporte,
        # no una consulta). Antes se devolvía 200 y el mensaje se perdía.
        tipo, file_id = adjunto
        pie = message.get("caption")
        text = pie if isinstance(pie, str) and pie.strip() else ATTACHMENT_PLACEHOLDERS[tipo]
        item = {**item, "text": text, "type": tipo, "file_id": file_id}
        log("INFO", "mensaje con adjunto para el buzón", update_id=update_id,
            message_type=tipo, has_caption=text != ATTACHMENT_PLACEHOLDERS[tipo],
            has_file_id=file_id is not None)
    elif not isinstance(text, str) or not text.strip():
        return
    command = None if adjunto is not None else parse_command(text)
    if command is None:
        try:
            append_inbox(web, item)
        except Exception as exc:
            log("ERROR", "fallo guardando en el buzón", update_id=update_id, error=str(exc))
            if raise_on_error:
                raise InboxWriteError(f"update {update_id} sin guardar en el buzón") from None
            return
    try:
        if command is None:
            api.send_message(chat_id, ACK_UNKNOWN)
        else:
            api.send_message(chat_id, reply_for(command, web, entrada.ahora))
        log("INFO", "mensaje procesado", update_id=update_id, command=command or "inbox")
    except Exception as exc:
        log("ERROR", "fallo procesando mensaje", update_id=update_id, error=str(exc))


def _report_wanted(cfg: rep.ReportConfig, entrada: _Incoming) -> bool:
    """¿Es un reporte? Nunca de bots; en grupo, solo menciones/respuestas al bot."""
    autor = entrada.message.get("from")
    if isinstance(autor, dict) and autor.get("is_bot"):
        return False
    if not entrada.in_group:
        return True
    if cfg.group_chat_ids and entrada.chat_id not in cfg.group_chat_ids:
        log("WARNING", "grupo no permitido para reportes, se ignora",
            update_id=entrada.update_id, chat_id=entrada.chat_id)
        return False
    # Charla del grupo: ni se guarda ni se registra.
    return rep.addressed_to_bot(entrada.message, cfg.bot_id, cfg.bot_username)


def _reply_in_thread(api: TelegramApi, entrada: _Incoming, texto: str) -> None:
    """Contesta citando el mensaje (y en su tema, si el grupo usa temas). Nunca lanza."""
    message = entrada.message
    message_id = message.get("message_id")
    hilo = message.get("message_thread_id") if message.get("is_topic_message") else None
    try:
        api.send_message(
            entrada.chat_id, texto,
            reply_to=message_id if isinstance(message_id, int) else None,
            thread_id=hilo if isinstance(hilo, int) else None,
        )
    except Exception as exc:
        log("ERROR", "fallo contestando un reporte", update_id=entrada.update_id, error=str(exc))


def _save_report(
    cfg: rep.ReportConfig, entrada: _Incoming, contenido: tuple[str, str, str | None]
) -> str | None:
    """Guarda el reporte respetando dedupe y límite. Devuelve qué contestar (o nada)."""
    if rep.already_stored(cfg.store, entrada.ahora, entrada.update_id):
        return None  # reentrega de Telegram: ya guardado y contestado
    source = "group" if entrada.in_group else "private"
    autor = entrada.message.get("from")
    usuario = autor.get("id") if isinstance(autor, dict) else None
    limite = rep.rate_check(cfg.store, str(usuario or entrada.chat_id), entrada.ahora)
    if limite != rep.RATE_OK:
        log("WARNING", "reporte de viajero por encima del límite", update_id=entrada.update_id,
            source=source, warned=limite == rep.RATE_WARN)
        return rep.RATE_LIMITED_RIDER if limite == rep.RATE_WARN else None
    texto, tipo, file_id = contenido
    informe = rep.build_report(ts=entrada.ahora, update_id=entrada.update_id, message=entrada.message,
                               source=source, text=texto, tipo=tipo, file_id=file_id)
    if not rep.store_report(cfg.store, informe, entrada.ahora):
        return None
    # Sin usuario ni texto en el log: el reporte ya está en el bucket privado.
    log("INFO", "reporte de viajero guardado", update_id=entrada.update_id,
        source=source, chat_type=entrada.chat_type, message_type=tipo)
    return rep.ACK_RIDER


def _handle_report(
    api: TelegramApi, cfg: rep.ReportConfig, entrada: _Incoming, *, raise_on_error: bool
) -> None:
    """Reporte de viajero (RB-020): privado de cualquiera o mención en un grupo."""
    if not _report_wanted(cfg, entrada):
        return
    contenido = rep.content(entrada.message)
    if contenido is None:
        return
    if not entrada.in_group and contenido[1] == "text" and rep.is_start(contenido[0]):
        _reply_in_thread(api, entrada, rep.WELCOME_RIDER)
        return
    try:
        respuesta = _save_report(cfg, entrada, contenido)
    except Exception as exc:
        log("ERROR", "fallo guardando un reporte de viajero", update_id=entrada.update_id, error=str(exc))
        if raise_on_error:
            raise InboxWriteError(f"update {entrada.update_id}: reporte sin guardar") from None
        return
    if respuesta is not None:
        _reply_in_thread(api, entrada, respuesta)


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
