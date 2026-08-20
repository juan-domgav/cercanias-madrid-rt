"""Tablón de salidas: horario programado + retraso publicado.

Hasta ahora esta lógica sólo existía en JavaScript dentro de la página, donde no
hay tests. El bot necesita lo mismo, así que vive aquí: una implementación,
probada, y la interfaz web queda como un consumidor más.

Reglas que no son evidentes y que costaron descubrir:

- **Un tren que termina en esta estación no es una salida.** En un final de línea
  como Colmenar Viejo, la mitad de las paradas del horario son llegadas. Listarlas
  como salidas es sencillamente falso.
- **El andén sólo existe en el feed en vivo** (Anexo A.6), nunca en el horario. Si
  el tren no está localizado todavía, se dice que no se sabe en vez de inventarlo:
  mandar a alguien al andén equivocado es peor que no decir nada.
- **El destino es la última parada del viaje**, porque ``trip_headsign`` viene
  vacío en el GTFS de Renfe.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Los minutos del día pueden pasar de 1440: GTFS expresa los servicios que
#: cruzan medianoche como 24:15, 25:03… (riesgo R-4).
MINUTES_PER_DAY = 1440

#: Longitud del prefijo de servicio: 4 digitos de calendario + letra del dia
#: (p. ej. "1020L"). Ver ADR-012.
SERVICE_PREFIX_LEN = 5

#: Margen de seguridad sobre la maniobra prevista. El tren tiene que llegar,
#: descargar, cambiar de cabina y salir; incluso "a su hora" hay unos minutos que
#: no se pueden comprimir.
TURNAROUND_SAFETY_MARGIN_MINUTES = 2

#: Suelo absoluto: por debajo de esto, avisar es ruido aunque no haya maniobra.
INBOUND_WARNING_FLOOR_MINUTES = 3


@dataclass(frozen=True)
class ScheduledStop:
    """Una parada del horario estático en una estación concreta."""

    trip_id: str
    time_hhmmss: str
    route: str
    destination: str | None
    terminates_here: bool
    #: Viaje que trae físicamente el tren que hará este servicio. En cabecera, un
    #: tren termina su recorrido, maniobra y sale en sentido contrario con otro
    #: número. Si el entrante llega tarde, el saliente no puede salir a su hora.
    #:
    #: Sólo se rellena cuando el emparejamiento es **1:1 y de la misma familia de
    #: línea**. La primera versión emparejaba por mera proximidad temporal y
    #: asignaba el mismo tren a varias salidas: 142 entrantes repartidos entre 302
    #: salidas, incluido un C1 que "formaba" a la vez un C3, un C2 y un C1. Tres
    #: andenes habrían mostrado el mismo aviso y dos habrían mentido.
    inbound_trip_id: str | None = None
    #: Minutos de maniobra previstos para ESTE enlace (0 a 40 en el horario real).
    #: El umbral tiene que ser relativo a él: con 35 minutos de margen un retraso
    #: de 10 se absorbe entero, y con 0 minutos cualquier retraso es crítico.
    turnaround_slack_minutes: int | None = None


@dataclass(frozen=True)
class LiveTrain:
    """Lo que el feed sabe ahora mismo de un tren."""

    trip_id: str
    delay_seconds: int | None
    platform: str | None


@dataclass(frozen=True)
class Departure:
    scheduled_hhmm: str
    expected_hhmm: str
    route: str
    destination: str | None
    train_number: str
    delay_minutes: int | None
    platform: str | None
    minutes_until: int
    located: bool
    #: Retraso del enlace entrante, en minutos, cuando este servicio todavía no
    #: circula. Es un AVISO, no una predicción: no se suma a la hora prevista
    #: porque parte del retraso lo absorbe la maniobra y porque a veces el
    #: servicio se suprime. Decir "viene con +25" es honesto; decir "sale a las
    #: 09:30" cuando en realidad no se sabe, no lo es.
    inbound_delay_minutes: int | None = None

    @property
    def at_risk(self) -> bool:
        """¿Hay motivo fundado para dudar de la hora prevista?"""
        return self.delay_minutes is None and bool(self.inbound_delay_minutes)

    @property
    def on_time(self) -> bool:
        return self.delay_minutes is not None and self.delay_minutes <= 0


def parse_hhmm_to_minutes(value: str) -> int:
    """``"25:14:00"`` -> 1514. Admite horas >= 24 (R-4)."""
    parts = value.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def format_minutes(total: int) -> str:
    total %= MINUTES_PER_DAY
    return f"{total // 60:02d}:{total % 60:02d}"


def train_number(trip_id: str) -> str:
    """``1020L20432C4b`` -> ``20432``. Sin esto, tres trenes de la misma línea
    aparecen como tres tarjetas idénticas y parecen el mismo tren repetido."""
    rest = trip_id[SERVICE_PREFIX_LEN:] if len(trip_id) > SERVICE_PREFIX_LEN else trip_id
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        elif digits:
            break
    return digits or trip_id


@dataclass(frozen=True)
class DepartureQuery:
    """Parámetros de la consulta. Agrupados para no arrastrar ocho argumentos."""

    now_minutes: int
    service_prefix: str
    destination: str | None = None
    window_minutes: int = 120
    #: Un tren que acaba de salir sigue siendo información útil para quien llega
    #: corriendo al andén.
    grace_minutes: int = 3
    limit: int = 6


def next_departures(
    scheduled: list[ScheduledStop],
    live: dict[str, LiveTrain],
    query: DepartureQuery,
) -> tuple[Departure, ...]:
    """Próximas salidas, ordenadas por hora prevista."""
    now_minutes = query.now_minutes
    service_prefix = query.service_prefix
    destination = query.destination
    window_minutes = query.window_minutes
    grace_minutes = query.grace_minutes
    limit = query.limit
    salidas: list[Departure] = []

    for stop in scheduled:
        if not stop.trip_id.startswith(service_prefix):
            continue
        if stop.terminates_here:
            continue
        if destination and stop.destination != destination:
            continue

        programada = parse_hhmm_to_minutes(stop.time_hhmmss)
        if programada < now_minutes - grace_minutes:
            continue
        if programada > now_minutes + window_minutes:
            continue

        tren = live.get(stop.trip_id)
        retraso = None
        if tren is not None and tren.delay_seconds is not None:
            retraso = round(tren.delay_seconds / 60)

        # El aviso del enlace SOLO aplica mientras no haya dato propio: en cuanto
        # el tren aparece con su retraso, ese dato manda y el del enlace sobra.
        retraso_enlace = None
        if retraso is None and stop.inbound_trip_id:
            entrante = live.get(stop.inbound_trip_id)
            if entrante is not None and entrante.delay_seconds is not None:
                minutos = round(entrante.delay_seconds / 60)
                # El umbral es el margen REAL de este enlace, no una mediana de
                # poblacion: con 35 min de maniobra un retraso de 10 se absorbe;
                # con 0 min, cualquier retraso material deja el anden vacio.
                margen = stop.turnaround_slack_minutes
                umbral = (
                    max(margen + TURNAROUND_SAFETY_MARGIN_MINUTES, INBOUND_WARNING_FLOOR_MINUTES)
                    if margen is not None
                    else INBOUND_WARNING_FLOOR_MINUTES
                )
                if minutos > umbral:
                    retraso_enlace = minutos

        salidas.append(
            Departure(
                scheduled_hhmm=format_minutes(programada),
                expected_hhmm=format_minutes(programada + (retraso or 0)),
                route=stop.route,
                destination=stop.destination,
                train_number=train_number(stop.trip_id),
                delay_minutes=retraso,
                platform=tren.platform if tren else None,
                minutes_until=programada + (retraso or 0) - now_minutes,
                located=tren is not None,
                inbound_delay_minutes=retraso_enlace,
            )
        )

    salidas.sort(key=lambda d: parse_hhmm_to_minutes(d.expected_hhmm))
    return tuple(salidas[:limit])


def destinations_available(scheduled: list[ScheduledStop], *, service_prefix: str) -> tuple[str, ...]:
    """Destinos con salida real desde esta estación.

    En una estación de paso conviven los dos sentidos; sin distinguirlos el
    tablón no sirve para decidir nada.
    """
    destinos = {
        s.destination
        for s in scheduled
        if s.trip_id.startswith(service_prefix) and not s.terminates_here and s.destination
    }
    return tuple(sorted(d for d in destinos if d))
