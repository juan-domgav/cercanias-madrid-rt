"""Reportes de viajeros al bot de ops (RB-020).

El bot de ops (``@CercaniasRTOPS_bot``) se anuncia en la comunidad pública
``t.me/cercanias_madrid_rt`` para que los viajeros cuenten fallos de la app:

- **Privado**, cualquier usuario: el mensaje es un reporte. Los comandos de ops
  (``estado``, ``horarios``...) siguen siendo SOLO del chat de Juan
  (``OPS_ALLOWED_CHAT_IDS``); a un viajero no se le ejecuta ninguno.
- **Grupo / supergrupo**: solo lo que MENCIONA al bot (entidad ``mention`` o
  ``text_mention``) o RESPONDE a un mensaje suyo. Todo lo demás se ignora en
  silencio. Nunca hay comandos desde un grupo. Con el modo privacidad de
  Telegram activado (por defecto) el bot ni siquiera recibe el resto.

Cada reporte lleva usuario y chat, así que va al bucket PRIVADO, un objeto por
mensaje: ``ops/reports/<día UTC>/<update_id>.json``. Sin tope (no se pierde
nada), dedupe natural por ``update_id`` y escritura "solo si no existe". El
buzón público ``ops/inbox.json`` sigue siendo solo de Juan.

Límite por usuario: ``RATE_MAX_REPORTS`` en ``RATE_WINDOW_S``; al pasarse se
avisa una vez y después se calla hasta que la ventana se vacía.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from cercanias_storage.publishers import PreconditionFailedError

REPORTS_PREFIX = "ops/reports/"
RATE_KEY = "ops/report-rate.json"
RATE_MAX_REPORTS = 5
RATE_WINDOW_S = 600
RATE_CAS_ATTEMPTS = 5
REPORT_MAX_CHARS = 2000
DEFAULT_BOT_USERNAME = "CercaniasRTOPS_bot"

ACK_RIDER = "Gracias, recibido. Lo revisamos."
WELCOME_RIDER = (
    "Hola. Cuéntame qué falla en la app de Cercanías Madrid RT: estación, línea, "
    "hora y qué ves. Si puedes, manda una captura. Lo revisamos."
)
RATE_LIMITED_RIDER = "Has enviado varios mensajes seguidos. Espera unos minutos antes de mandar más."

#: Primer mensaje de cualquiera que abre el bot: se contesta, no es un reporte.
_START_COMMANDS = frozenset({"/start", "/help", "/ayuda"})

#: Adjuntos -> marcador si no hay pie (mismo criterio que el buzón, PR #26).
ATTACHMENT_PLACEHOLDERS = {
    "photo": "[foto]",
    "document": "[documento]",
    "video": "[vídeo]",
    "animation": "[animación]",
    "voice": "[nota de voz]",
    "audio": "[audio]",
    "video_note": "[vídeo]",
    "sticker": "[sticker]",
}


class ReportStore(Protocol):
    def read(self, key: str) -> bytes | None: ...

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


@dataclass(frozen=True)
class ReportConfig:
    """Lo que hace falta para aceptar reportes. ``store`` = bucket PRIVADO."""

    store: ReportStore
    bot_id: int | None
    bot_username: str = DEFAULT_BOT_USERNAME
    #: Grupos de los que se aceptan menciones. Vacío = cualquiera (el límite por
    #: usuario frena el abuso); fijarlo cuando se conozca el id de la comunidad.
    group_chat_ids: frozenset[int] = field(default_factory=frozenset)


def bot_id_from_token(token: str) -> int | None:
    """El id del bot es la parte numérica del token, antes de ``:``. No se registra el token."""
    prefijo = token.split(":", 1)[0] if ":" in token else ""
    return int(prefijo) if prefijo.isdigit() else None


def attachment(message: dict[str, Any]) -> tuple[str, str | None] | None:
    """``(tipo, file_id)`` del adjunto, o ``None``. De una foto, la mayor resolución."""
    for tipo in ATTACHMENT_PLACEHOLDERS:
        valor = message.get(tipo)
        if isinstance(valor, list):
            valor = valor[-1] if valor else None
        if isinstance(valor, dict):
            file_id = valor.get("file_id")
            return tipo, file_id if isinstance(file_id, str) and file_id else None
    return None


def content(message: dict[str, Any]) -> tuple[str, str, str | None] | None:
    """``(texto, tipo, file_id)`` del mensaje, o ``None`` si no trae nada útil."""
    text = message.get("text")
    if isinstance(text, str) and text.strip():
        return text, "text", None
    adjunto = attachment(message)
    if adjunto is None:
        return None
    tipo, file_id = adjunto
    pie = message.get("caption")
    return (pie if isinstance(pie, str) and pie.strip() else ATTACHMENT_PLACEHOLDERS[tipo]), tipo, file_id


def _utf16_slice(text: str, offset: int, length: int) -> str:
    """Telegram da ``offset``/``length`` en unidades UTF-16 (un emoji cuenta 2)."""
    raw = text.encode("utf-16-le")
    return raw[offset * 2 : (offset + length) * 2].decode("utf-16-le", errors="ignore")


def addressed_to_bot(message: dict[str, Any], bot_id: int | None, bot_username: str) -> bool:
    """¿El mensaje de grupo menciona al bot o responde a un mensaje suyo?"""
    respuesta = message.get("reply_to_message")
    if isinstance(respuesta, dict):
        autor = respuesta.get("from") or {}
        if isinstance(autor, dict) and (
            (bot_id is not None and autor.get("id") == bot_id)
            or str(autor.get("username") or "").lower() == bot_username.lower()
        ):
            return True
    objetivo = f"@{bot_username}".lower()
    for texto_key, ent_key in (("text", "entities"), ("caption", "caption_entities")):
        texto = message.get(texto_key)
        entidades = message.get(ent_key)
        if not isinstance(texto, str) or not isinstance(entidades, list):
            continue
        for e in entidades:
            if not isinstance(e, dict):
                continue
            if e.get("type") == "mention":
                off, length = e.get("offset"), e.get("length")
                if isinstance(off, int) and isinstance(length, int) and \
                        _utf16_slice(texto, off, length).lower() == objetivo:
                    return True
            if e.get("type") == "text_mention" and bot_id is not None and \
                    (e.get("user") or {}).get("id") == bot_id:
                return True
    return False


def is_start(text: str) -> bool:
    words = text.strip().split()
    return bool(words) and words[0].lower().split("@", 1)[0] in _START_COMMANDS


def report_key(ts: datetime, update_id: int) -> str:
    return f"{REPORTS_PREFIX}{ts.date().isoformat()}/{update_id}.json"


def build_report(  # noqa: PLR0913 - campos del reporte, todos con nombre
    *, ts: datetime, update_id: int, message: dict[str, Any], source: str,
    text: str, tipo: str, file_id: str | None,
) -> dict[str, Any]:
    chat = message.get("chat") or {}
    autor = message.get("from") or {}
    return {
        "ts": ts.isoformat(),
        "update_id": update_id,
        "message_id": message.get("message_id"),
        "text": text[:REPORT_MAX_CHARS],
        "source": source,
        "chat_id": chat.get("id"),
        "chat_type": chat.get("type"),
        "chat_title": chat.get("title"),
        "from_id": autor.get("id"),
        "from_username": autor.get("username"),
        "type": tipo,
        "file_id": file_id,
    }


def already_stored(store: ReportStore, ts: datetime, update_id: int) -> bool:
    return store.read(report_key(ts, update_id)) is not None


def store_report(store: ReportStore, report: dict[str, Any], ts: datetime) -> bool:
    """Guarda el reporte; ``False`` si ya existía (reentrega). Lanza si falla la escritura."""
    try:
        store.publish(
            report_key(ts, int(report["update_id"])),
            json.dumps(report, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
            cache_control="no-store",
            if_generation_match=0,
        )
    except PreconditionFailedError:
        return False
    return True


#: Resultado del límite: aceptar, avisar (primera vez que se pasa) o callar.
RATE_OK = "ok"
RATE_WARN = "warn"
RATE_SILENT = "silent"


def _rate_decide(data: Any, user: str, now_s: float) -> tuple[dict[str, Any], str]:
    estado = data if isinstance(data, dict) else {}
    limpio: dict[str, Any] = {}
    for k, v in estado.items():
        if not isinstance(v, dict):
            continue
        marcas = [t for t in v.get("ts") or [] if isinstance(t, (int, float)) and now_s - t < RATE_WINDOW_S]
        if marcas:
            limpio[k] = {"ts": marcas, "warned": bool(v.get("warned"))}
    actual = limpio.get(user, {"ts": [], "warned": False})
    if len(actual["ts"]) >= RATE_MAX_REPORTS:
        resultado = RATE_SILENT if actual["warned"] else RATE_WARN
        actual = {**actual, "warned": True}
    else:
        resultado = RATE_OK
        actual = {"ts": [*actual["ts"], now_s], "warned": False}
    limpio[user] = actual
    return limpio, resultado


def rate_check(store: ReportStore, user: str, now: datetime) -> str:
    """Apunta el intento y dice si se acepta. Escritura condicional (varias entregas a la vez)."""
    for _ in range(RATE_CAS_ATTEMPTS):
        try:
            raw, generation = store.read_versioned(RATE_KEY)
        except PreconditionFailedError:
            continue
        try:
            data = json.loads(raw) if raw else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = None
        nuevo, resultado = _rate_decide(data, user, now.timestamp())
        try:
            store.publish(
                RATE_KEY, json.dumps(nuevo).encode("utf-8"), content_type="application/json",
                cache_control="no-store", if_generation_match=generation,
            )
        except PreconditionFailedError:
            continue
        return resultado
    # Sin poder apuntarlo, se acepta: perder un reporte es peor que un spam puntual.
    return RATE_OK
