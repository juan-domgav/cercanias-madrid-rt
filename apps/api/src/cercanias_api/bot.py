"""Lógica del bot de Telegram, sin HTTP ni almacenamiento.

Recibe un *update* ya deserializado y devuelve qué responder. Así se puede probar
cada comando sin levantar el servicio ni hablar con Telegram.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any

from cercanias_domain.station_view import Station, nearest_stations

MAX_FEEDBACK_CHARS = 1000

AYUDA = (
    "<b>Cercanías Madrid</b>\n\n"
    "• <b>/proximos <i>estación</i></b> — próximas salidas con andén y destino\n"
    "   <i>ejemplo: /proximos Tres Cantos</i>\n"
    "• <b>Enviar ubicación</b> — estaciones cercanas (botón de abajo)\n"
    "• <b>/bug <i>lo que falla</i></b> — cuéntame un fallo y queda registrado\n\n"
    "<i>Datos de Renfe (CC BY 4.0). Proyecto independiente, sin relación con "
    "Renfe ni Adif.</i>"
)

#: Teclado con el botón nativo de ubicación. Es mejor que la geolocalización del
#: navegador: usa el diálogo del sistema y no depende de permisos por origen web.
TECLADO_UBICACION: dict[str, Any] = {
    "keyboard": [[{"text": "📍 Estaciones cerca de mí", "request_location": True}]],
    "resize_keyboard": True,
}


@dataclass(frozen=True)
class Reply:
    text: str
    reply_markup: dict[str, Any] | None = None


@dataclass(frozen=True)
class Feedback:
    """Un aviso de fallo. Se guarda lo mínimo para poder responder y reproducir."""

    chat_id: int
    text: str
    station_hint: str | None = None


@dataclass
class BotResult:
    reply: Reply | None = None
    feedback: Feedback | None = None
    extra: list[str] = field(default_factory=list)


def normalize(name: str) -> str:
    """Compara nombres de estación ignorando tildes, mayúsculas y puntuación."""
    sin_tildes = "".join(
        c for c in unicodedata.normalize("NFD", name.lower()) if unicodedata.category(c) != "Mn"
    )
    return "".join(c if c.isalnum() else " " for c in sin_tildes).strip()


def find_station(query: str, stations: list[Station]) -> Station | None:
    """Busca por nombre: exacto, luego por prefijo, luego por contenido."""
    q = normalize(query)
    if not q:
        return None
    exactos = [s for s in stations if normalize(s.name) == q]
    if exactos:
        return exactos[0]
    empieza = [s for s in stations if normalize(s.name).startswith(q)]
    if empieza:
        return min(empieza, key=lambda s: len(s.name))
    contiene = [s for s in stations if q in normalize(s.name)]
    return min(contiene, key=lambda s: len(s.name)) if contiene else None


def _format_special(d: dict[str, Any]) -> str:
    """Tren especial (SPECIAL_10_*): sin horario, asi que sin hora de salida.

    Mismo criterio que la web: no se pinta una hora como si fuera de horario ni
    "sin datos en vivo" (esta localizado); se dice donde esta y en que anden.
    """
    donde = "en el andén" if d.get("status") == "STOPPED_AT" else "llegando"
    anden = f"Andén <b>{d['platform']}</b>" if d.get("platform") else "Andén —"
    estado = "SUPRIMIDO" if d.get("cancelled") else "tren especial"
    texto = (
        f"<b>{donde}</b> → {d.get('dest') or 'destino desconocido'}\n"
        f"   {d.get('route', '')} tren {d.get('train', '')} · {anden} · {estado}"
    )
    if d.get("at_terminus"):
        texto += "\n   🏁 final de línea: puede ser una llegada, no una salida"
    return texto


def format_departures(station: Station, salidas: list[dict[str, Any]], destinos: list[str]) -> str:
    if not salidas:
        return f"<b>{station.name}</b>\nNo hay salidas programadas en las próximas 2 horas."
    lineas = [f"<b>{station.name}</b>"]
    if len(destinos) > 1:
        lineas.append(f"<i>Sentidos: {' · '.join(destinos)}</i>")
    lineas.append("")
    for d in salidas[:6]:
        if d.get("special"):
            lineas.append(_format_special(d))
            continue
        # RB-007: retraso ESTIMADO desde el entrante; nunca como dato en vivo.
        estimado = d.get("delay_source") == "inbound_estimate"
        hora = f"<b>{'≈ ' if estimado else ''}{d['eta']}</b>"
        if d.get("delay") or (estimado and d.get("estimated_delay")):
            hora += f" <s>{d['t']}</s>"
        retraso = (
            "SUPRIMIDO" if d.get("cancelled")
            else (f"≈ +{d['estimated_delay']} min (estimado)" if d.get("estimated_delay")
                  else "≈ en hora (estimado)") if estimado
            # RB-007: fuera de la ventana de publicacion no es una anomalia.
            else "programado · Renfe aún no lo publica"
            if d.get("realtime_status") == "not_published"
            else "sin datos en vivo" if d.get("delay") is None
            else "en hora" if d.get("on_time")
            else f"+{d['delay']} min" if d["delay"] > 0
            else "sin confirmar"
        )
        anden = f"Andén <b>{d['platform']}</b>" if d.get("platform") else "Andén —"
        lineas.append(
            f"{hora} → {d.get('dest') or '?'}\n"
            f"   {d.get('route','')} tren {d.get('train','')} · {anden} · {retraso}"
        )
        if d.get("inbound_delay"):
            lineas.append(
                f"   ⚠️ el tren que hace este servicio viene con +{d['inbound_delay']} min"
            )
        elif d.get("inbound_cancelled"):
            lineas.append("   🚫 el tren que debia hacer este servicio esta SUPRIMIDO")
        elif d.get("shortened"):
            lineas.append("   ✂️ no efectua todas sus paradas: puede no llegar al destino")
        elif estimado:
            lineas.append("   ≈ estimado por el tren que llega a cabecera; aún sin datos en vivo")
        elif d.get("unconfirmed"):
            lineas.append("   ❓ sin confirmar: no localizamos este tren ni el que deberia traerlo")
    lineas.append("\n<i>Andén y retraso vienen del feed en vivo; si el tren aún no "
                  "está localizado, no se muestran.</i>")
    return "\n".join(lineas)


@dataclass(frozen=True)
class BotContext:
    """Todo lo que el bot necesita saber del mundo para contestar.

    ``boards`` es el tablon ya calculado por la ingesta: el bot pinta, no calcula.
    """

    stations: list[Station]
    boards: dict[str, Any]


def _handle_location(location: dict[str, Any], ctx: BotContext) -> BotResult:
    cercanas = nearest_stations(
        float(location["latitude"]), float(location["longitude"]), ctx.stations, limit=4
    )
    if not cercanas:
        return BotResult(reply=Reply("No encuentro estaciones cerca de esa posición."))
    texto = ["<b>Estaciones más cercanas</b>", ""]
    for s, km in cercanas:
        texto.append(f"• {s.name} — {km:.1f} km\n   <code>/proximos {s.name}</code>")
    return BotResult(reply=Reply("\n".join(texto)))


def _handle_bug(resto: str, chat_id: int) -> BotResult:
    if not resto:
        return BotResult(reply=Reply(
            "Cuéntame qué falla después del comando.\n"
            "<i>ejemplo: /bug en Colmenar sale un tren que ya ha pasado</i>"))
    return BotResult(
        reply=Reply("Anotado, gracias. Queda registrado con la hora para poder revisarlo."),
        feedback=Feedback(chat_id=chat_id, text=resto[:MAX_FEEDBACK_CHARS]),
    )


def _handle_proximos(resto: str, ctx: BotContext) -> BotResult:
    if not resto:
        return BotResult(reply=Reply(
            "Dime la estación: <code>/proximos Tres Cantos</code>", TECLADO_UBICACION))
    station = find_station(resto, ctx.stations)
    if station is None:
        return BotResult(reply=Reply(
            f"No conozco ninguna estación que se parezca a «{resto}».\n"
            "Prueba a enviar tu ubicación con el botón de abajo.", TECLADO_UBICACION))
    est = (ctx.boards.get("stations") or {}).get(station.stop_id) or {}
    return BotResult(reply=Reply(format_departures(
        station, est.get("departures") or [], est.get("destinations") or [])))


def _message_and_chat(update: dict[str, Any]) -> tuple[dict[str, Any], int] | None:
    """Mensaje y chat validos, o ``None`` si el update no es de los que atendemos.

    Telegram envia muchos tipos de update (posts de canal, ediciones, callbacks);
    los que no se manejan se ignoran en silencio, sin responder 4xx: un error
    haria que Telegram reintentase indefinidamente.
    """
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    chat_id = (message.get("chat") or {}).get("id")
    if not isinstance(chat_id, int):
        return None
    return message, chat_id


def handle_update(update: dict[str, Any], ctx: BotContext) -> BotResult:
    """Traduce un update de Telegram en una respuesta."""
    parsed = _message_and_chat(update)
    if parsed is None:
        return BotResult()
    message, chat_id = parsed

    # Ubicación nativa: no hace falta que el usuario escriba nada.
    location = message.get("location")
    if isinstance(location, dict) and "latitude" in location:
        return _handle_location(location, ctx)

    text = (message.get("text") or "").strip()
    if not text:
        return BotResult()

    # `/comando@NombreDelBot` es la forma que usa Telegram en grupos.
    comando, _, resto = text.partition(" ")
    comando = comando.split("@")[0].lower()
    resto = resto.strip()

    if comando == "/bug":
        return _handle_bug(resto, chat_id)
    if comando == "/proximos":
        return _handle_proximos(resto, ctx)
    # Cualquier otra cosa, incluidos /start y /help, recibe la ayuda: es mas util
    # que un "no te entiendo" a quien acaba de abrir el bot.
    return BotResult(reply=Reply(AYUDA, TECLADO_UBICACION))
