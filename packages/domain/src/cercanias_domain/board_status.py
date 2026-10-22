"""Estado presentable de una salida del tablón: UNA función para todos (RB-008).

El bot, la web y la mini app pintaban cada uno su propia cascada de ``if`` sobre
las mismas columnas de ``boards.json``. Divergían en los huecos: una fila con
``live: true``, ``delay: 0`` y ``on_time: false`` caía en el ``else`` final y salía
como "sin confirmar" con el tren localizado en el andén. Aquí se decide una vez:

- **Precedencia**: suprimido > especial > EN VIVO (ya salió) > PENDIENTE DE SALIR
  (en el feed pero aún en cabecera, RB-009) > estimado desde el entrante > no
  publicado > sin datos. Si el propio viaje circula, su dato manda.
- **Pendiente de salir no es "en hora"** (RB-009): antes de salir de cabecera
  Renfe publica 0 o el retraso que arrastra la unidad, y en el archivo del
  19-22/10/2026 el 54 % de esos "0" salieron con >= 3 min. Se dice
  "pendiente de salir · Renfe aún no da retraso"; pasada su hora, "+N min · aún
  sin salir" (lo que ya lleva esperando, cota inferior cierta).
- **Con el viaje en vivo nunca** se dice "sin confirmar", ni "el tren que hace
  este servicio viene con +N", ni "el que debía hacerlo está suprimido": los tres
  contradicen el dato en vivo. Se filtran aquí aunque la fila los traiga (p. ej.
  un ``boards.json`` generado por una versión anterior de la ingesta).

La ingesta guarda el resultado en ``boards.json`` (``display``) para que la web
sólo lo pinte; el bot llama a esta misma función sobre la fila.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Por encima de esto un retraso en vivo se pinta como "tarde" (color). Renfe
#: considera puntual un Cercanías con menos de tres minutos.
LATE_TONE_MINUTES = 3

KIND_CANCELLED = "cancelled"
KIND_SPECIAL = "special"
KIND_LIVE = "live"
KIND_PENDING_DEPARTURE = "pending_departure"
KIND_INBOUND_ESTIMATE = "inbound_estimate"
KIND_NOT_PUBLISHED = "not_published"
KIND_NO_DATA = "no_data"


@dataclass(frozen=True)
class Notice:
    """Aviso bajo la salida. ``text`` es la versión corta (bot), ``detail`` la larga (web)."""

    kind: str
    icon: str
    text: str
    detail: str

    def to_json(self) -> dict[str, str]:
        return {"kind": self.kind, "icon": self.icon, "text": self.text, "detail": self.detail}


@dataclass(frozen=True)
class DepartureStatus:
    kind: str
    #: Etiqueta de estado: "en hora", "+4 min", "sin datos en vivo"...
    label: str
    #: ``cancelled`` | ``late`` | ``ontime`` | ``estimated`` | ``neutral``.
    tone: str
    #: La hora mostrada es una estimación: se antepone "≈".
    approximate: bool
    #: Tachar la hora programada junto a la prevista (hay desfase).
    show_scheduled: bool
    notices: tuple[Notice, ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "label": self.label,
            "tone": self.tone,
            "approximate": self.approximate,
            "show_scheduled": self.show_scheduled,
            "notices": [n.to_json() for n in self.notices],
        }


def is_live(row: Mapping[str, Any]) -> bool:
    """¿Está el propio viaje en el feed?

    ``live`` lo dice la ingesta. Una fila antigua sin ``live`` pero con ``delay``
    también lo está: ``delay`` sólo se rellena desde el feed, nunca se estima.
    """
    if row.get("special"):
        return True
    if "live" in row:
        return bool(row.get("live"))
    return row.get("delay") is not None


def is_pending_departure(row: Mapping[str, Any]) -> bool:
    """En el feed pero aún en su estación de origen sin salir (RB-009)."""
    return bool(row.get("pending_departure")) or row.get("realtime_status") == "pending_departure"


def is_measured(row: Mapping[str, Any]) -> bool:
    """¿Hay un retraso MEDIDO del propio viaje? (circulando, o ya pasada su hora en cabecera)."""
    return is_live(row) and not (is_pending_departure(row) and row.get("delay") is None)


def _kind(row: Mapping[str, Any], live: bool) -> str:
    estimado = (
        row.get("delay_source") == "inbound_estimate"
        or row.get("realtime_status") == "inbound_estimate"
    )
    reglas = (
        (bool(row.get("cancelled")), KIND_CANCELLED),
        (bool(row.get("special")), KIND_SPECIAL),
        (live and is_pending_departure(row), KIND_PENDING_DEPARTURE),
        (live, KIND_LIVE),
        (estimado, KIND_INBOUND_ESTIMATE),
        (row.get("realtime_status") == "not_published", KIND_NOT_PUBLISHED),
    )
    return next((kind for cumple, kind in reglas if cumple), KIND_NO_DATA)


#: Etiquetas que no dependen de ningún número.
_FIXED_LABELS: dict[str, tuple[str, str]] = {
    KIND_CANCELLED: ("SUPRIMIDO", "cancelled"),
    KIND_SPECIAL: ("tren especial", "neutral"),
    KIND_NOT_PUBLISHED: ("programado · Renfe aún no lo publica", "neutral"),
    KIND_NO_DATA: ("sin datos en vivo", "neutral"),
}


def _live_label_tone(delay: int | None) -> tuple[str, str]:
    if delay is None:
        return "localizado · sin retraso publicado", "neutral"
    if delay > 0:
        return f"+{delay} min", "late" if delay > LATE_TONE_MINUTES else "ontime"
    # 0 o negativo (adelantado): en hora. Antes un 0 "en riesgo" caía en
    # "sin confirmar"; con el tren en el feed no hay nada sin confirmar.
    return "en hora", "ontime"


def _pending_label_tone(row: Mapping[str, Any]) -> tuple[str, str]:
    delay = row.get("delay")
    if delay is not None and delay > 0:
        # Pasada su hora y aún en el andén: lo que lleva es seguro, puede ser más.
        return f"+{delay} min · aún sin salir", "late" if delay > LATE_TONE_MINUTES else "ontime"
    if row.get("estimated_delay") is not None:
        est = row["estimated_delay"]
        return (f"≈ +{est} min (estimado)" if est > 0 else "≈ en hora (estimado)"), "estimated"
    return "pendiente de salir · Renfe aún no da retraso", "neutral"


def _label_tone(kind: str, row: Mapping[str, Any]) -> tuple[str, str]:
    if kind == KIND_LIVE:
        return _live_label_tone(row.get("delay"))
    if kind == KIND_PENDING_DEPARTURE:
        return _pending_label_tone(row)
    if kind == KIND_INBOUND_ESTIMATE:
        est = row.get("estimated_delay") or 0
        return (f"≈ +{est} min (estimado)" if est > 0 else "≈ en hora (estimado)"), "estimated"
    return _FIXED_LABELS[kind]


def _notices(kind: str, row: Mapping[str, Any], live: bool) -> tuple[Notice, ...]:
    avisos: list[Notice] = []
    if kind == KIND_CANCELLED:
        avisos.append(Notice(
            "cancelled", "🚫", "Renfe ha cancelado este servicio",
            "Tren suprimido. Renfe ha cancelado este servicio."))
        return tuple(avisos)
    if row.get("shortened"):
        avisos.append(Notice(
            "shortened", "✂️", "no efectúa todas sus paradas: puede no llegar al destino",
            f"Este tren no efectúa todas sus paradas. Puede que no llegue a "
            f"{row.get('dest') or 'su destino'}: confírmalo antes de subir."))
    if kind == KIND_SPECIAL:
        avisos.append(Notice(
            "special", "🚆", "tren especial sin horario publicado",
            "Tren especial sin horario publicado. Lo vemos en vivo; la hora es la de "
            "cuando se le vio, no la de un horario. Sin destino publicado: se muestra "
            "en cualquier sentido."))
    if row.get("at_terminus"):
        avisos.append(Notice(
            "at_terminus", "🏁", "final de línea: puede ser una llegada, no una salida",
            "Final de línea: puede ser una llegada, no una salida."))
    if kind == KIND_PENDING_DEPARTURE:
        if row.get("estimated_delay") is not None and row.get("delay") is None:
            avisos.append(Notice(
                "estimated", "≈", "estimado por el tren que llega a cabecera; aún no ha salido",
                "Estimado a partir del tren que llega a cabecera para dar la vuelta. "
                "Este tren aún no ha salido y Renfe no da su retraso hasta que sale."))
        if row.get("inbound_delay") and row.get("delay") is None:
            # Sólo llega aquí en líneas validadas (ORIGIN_ESTIMATE_ROUTES).
            n = row["inbound_delay"]
            avisos.append(Notice(
                "inbound_delay", "⚠️", f"posible retraso: el tren de llegada viene con +{n} min",
                f"Posible retraso: el tren que llega a cabecera viene con +{n} min."))
        return tuple(avisos)
    if live:
        # El dato en vivo manda: nada que lo contradiga (RB-008).
        return tuple(avisos)
    if kind == KIND_INBOUND_ESTIMATE:
        avisos.append(Notice(
            "estimated", "≈", "estimado por el tren que llega a cabecera; aún sin datos en vivo",
            "Estimado a partir del tren que llega a cabecera para dar la vuelta. "
            "Renfe aún no publica este viaje en vivo."))
    if row.get("inbound_cancelled"):
        avisos.append(Notice(
            "inbound_cancelled", "🚫", "el tren que debía hacer este servicio está SUPRIMIDO",
            "El tren que debía hacer este servicio está suprimido. Es probable que no salga."))
    elif row.get("inbound_delay"):
        n = row["inbound_delay"]
        avisos.append(Notice(
            "inbound_delay", "⚠️", f"el tren que hace este servicio viene con +{n} min",
            f"El tren que hace este servicio viene con +{n} min. Es probable que salga tarde."))
    elif row.get("unconfirmed"):
        avisos.append(Notice(
            "unconfirmed", "❓", "sin confirmar: no localizamos este tren ni el que debería traerlo",
            "Sin confirmar: no localizamos este tren ni el que debería traerlo. La hora es "
            "la del horario, no hay dato en vivo que la respalde."))
    return tuple(avisos)


def departure_status(row: Mapping[str, Any]) -> DepartureStatus:
    """Estado de una fila de ``boards.json`` con la precedencia única de RB-008."""
    live = is_live(row)
    kind = _kind(row, live)
    label, tone = _label_tone(kind, row)
    approximate = kind == KIND_INBOUND_ESTIMATE or (
        kind == KIND_PENDING_DEPARTURE
        and row.get("delay") is None
        and row.get("estimated_delay") is not None
    )
    show_scheduled = bool(
        (kind in (KIND_LIVE, KIND_PENDING_DEPARTURE) and row.get("delay"))
        or (approximate and row.get("estimated_delay"))
    )
    return DepartureStatus(
        kind=kind,
        label=label,
        tone=tone,
        approximate=approximate,
        show_scheduled=show_scheduled,
        notices=_notices(kind, row, live),
    )


def contradicts_live(row: Mapping[str, Any]) -> bool:
    """La fila (los datos crudos) trae un aviso que el dato en vivo desmiente.

    Invariante de la ingesta: debe ser siempre ``False``. Se cuenta en la salud de
    ``boards.json`` (``departures_contradicting_live``) para detectarlo en RB-008.

    Un tren localizado nunca es ``unconfirmed``. Un aviso de entrante sólo
    contradice un retraso MEDIDO: con el tren aún en cabecera antes de su hora
    (RB-009) no hay medida que contradecir.
    """
    if not is_live(row) or row.get("special"):
        return False
    if row.get("unconfirmed"):
        return True
    return is_measured(row) and bool(row.get("inbound_delay") or row.get("inbound_cancelled"))
