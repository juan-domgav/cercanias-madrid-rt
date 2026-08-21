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

#: Hasta esta hora local, los trenes que circulan pertenecen mayoritariamente al
#: día de servicio ANTERIOR: un tren de las 24:36 es de la jornada de ayer aunque
#: el reloj marque las 00:36. Cercanías Madrid no presta servicio entre ~01:30 y
#: ~05:00, así que 04:00 es un corte holgado y sin trenes que separar.
OPERATING_DAY_ROLLOVER_HOUR = 4

#: Longitud del prefijo de servicio: 4 digitos de calendario + letra del dia
#: (p. ej. "1020L"). Ver ADR-012.
SERVICE_PREFIX_LEN = 5

#: Minutos mínimos para dar la vuelta al tren: llegar, descargar, cambiar de
#: cabina y salir. Aunque el horario prevea maniobra 0, esto no se comprime.
MIN_PHYSICAL_TURNAROUND_MINUTES = 3

#: Margen de seguridad sobre la maniobra prevista. El tren tiene que llegar,
#: descargar, cambiar de cabina y salir; incluso "a su hora" hay unos minutos que
#: no se pueden comprimir.
TURNAROUND_SAFETY_MARGIN_MINUTES = 2

#: A partir de este adelanto, un servicio que no aparece en el feed ni tiene
#: enlace localizado deja de ser "todavía pronto" y pasa a ser desconocido. No es
#: una cancelación —no hay feed que lo diga— pero tampoco es una salida normal, y
#: presentarla con su hora limpia es la mentira que deja a alguien esperando.
UNCONFIRMED_LEAD_MINUTES = 5

#: Materialidad mínima. Renfe considera puntual un Cercanías con menos de tres
#: minutos, así que por debajo de eso avisar es ruido AUNQUE la maniobra sea 0 y
#: el andén esté técnicamente vacío. Se aplica siempre, no sólo cuando falta el
#: dato de maniobra: un aviso que no cambia ninguna decisión enseña a ignorar los
#: que sí la cambian.
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
    #: ``True`` sólo si esta parada es el ORIGEN del viaje. El enlace de cabecera
    #: sólo significa algo ahí: en una parada intermedia el tren ya circula y su
    #: propio retraso es el dato bueno.
    is_origin: bool = False
    #: Estación de origen, para comprobar que el retraso del entrante está medido
    #: precisamente ahí y no ocho paradas antes.
    origin_stop_id: str | None = None


@dataclass(frozen=True)
class LiveTrain:
    """Lo que el feed sabe ahora mismo de un tren."""

    trip_id: str
    delay_seconds: int | None
    platform: str | None
    #: Próxima parada declarada. Imprescindible para el aviso de enlace: el
    #: retraso publicado es el retraso EN ESA PARADA, que puede estar a ocho
    #: estaciones y cuarenta minutos de la cabecera. Sin esto, se presentaba un
    #: dato sobre Pitis como si fuera un dato sobre El Escorial.
    next_stop_id: str | None = None


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
    #: La hora se echa encima y no hay ni rastro del tren ni de su enlace. No
    #: afirma que esté suprimido —sin un feed de supresiones eso no se puede
    #: saber— pero sí que no hay nada que respalde esa hora.
    unconfirmed: bool = False

    @property
    def at_risk(self) -> bool:
        """¿Hay motivo fundado para dudar de la hora prevista?"""
        return bool(self.inbound_delay_minutes)

    @property
    def shows_on_time(self) -> bool:
        """¿Puede afirmarse "en hora"?

        Un ``delay`` de 0 en un viaje que aún no ha salido es un marcador de
        posición del feed, no una lectura. Con el enlace en riesgo, presentarlo
        como "en hora" es la peor forma de equivocarse: alguien se queda en el
        andén tranquilo mientras su tren está a dieciséis minutos.
        """
        return self.delay_minutes is not None and self.delay_minutes <= 0 and not self.at_risk

    @property
    def on_time(self) -> bool:
        return self.delay_minutes is not None and self.delay_minutes <= 0


def parse_hhmm_to_minutes(value: str) -> int:
    """``"25:14:00"`` -> 1514. Admite horas >= 24 (R-4)."""
    parts = value.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def now_in_operating_day(hour: int, minute: int) -> int:
    """Minutos desde el inicio de la jornada de servicio en curso.

    A las 00:36 devuelve 1476, no 36: así compara directamente con un horario que
    expresa ese mismo tren como ``24:36``. Sin esto, después de medianoche toda
    la madrugada desaparecía del tablón —un servicio de las 24:08 quedaba a
    1448 minutos de "ahora=8" y se descartaba por lejano— y el andén no mostraba
    un tren que estaba a punto de salir.
    """
    minutos = hour * 60 + minute
    if hour < OPERATING_DAY_ROLLOVER_HOUR:
        return minutos + MINUTES_PER_DAY
    return minutos


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


def _inbound_warning(
    stop: ScheduledStop, live: dict[str, LiveTrain], propio: LiveTrain | None
) -> int | None:
    """Minutos de retraso del enlace entrante, si procede avisar.

    Cinco condiciones, todas necesarias. Cada una corresponde a una forma
    concreta de mentirle a alguien que está en un andén.
    """
    # 1. Sólo en el origen del viaje. En una parada intermedia el tren ya circula
    #    y el enlace de cabecera no dice nada sobre él.
    if not stop.is_origin or not stop.inbound_trip_id:
        return None

    # 2. El dato propio manda... pero sólo si es un dato de verdad. Un `delay` de
    #    0 en un viaje que aún no ha salido es un marcador de posición, no una
    #    lectura: tomarlo por bueno pinta "en hora" con el tren a 25 minutos.
    if propio is not None and propio.delay_seconds:
        return None

    entrante = live.get(stop.inbound_trip_id)
    if entrante is None or entrante.delay_seconds is None:
        return None

    minutos = round(entrante.delay_seconds / 60)
    margen = stop.turnaround_slack_minutes

    # Sin maniobra conocida no se puede proyectar nada: sólo se avisa si el
    # retraso es grande de por sí.
    if margen is None:
        return minutos if minutos >= INBOUND_WARNING_FLOOR_MINUTES else None

    # La física del enlace, que no depende de DÓNDE se haya medido el retraso:
    # el entrante llega a cabecera a su hora prevista más su retraso, y todavía
    # necesita un mínimo para dar la vuelta. Lo que exceda de la maniobra
    # programada se traslada a la salida.
    #
    # Antes se exigía que el entrante estuviese YA en la cabecera. Eso eliminaba
    # el aviso justo cuando era útil: el 21/08 el tren del 16:12 de Colmenar iba
    # +16 con tres paradas por delante, no podía salir a su hora, y el andén leía
    # "En hora". Un aviso que sólo llega cuando el tren ya está entrando no sirve
    # para decidir nada.
    proyectado = minutos - margen + MIN_PHYSICAL_TURNAROUND_MINUTES
    return minutos if proyectado >= INBOUND_WARNING_FLOOR_MINUTES else None


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
        retraso_enlace = _inbound_warning(stop, live, tren)

        # Un tren que termina su recorrido desaparece del feed con normalidad: es
        # el caso NORMAL de una maniobra, no una señal de supresión. Por eso la
        # ausencia del entrante no genera aviso. Lo que sí se puede afirmar es que
        # no hay nada que respalde esta hora.
        sin_respaldo = (
            tren is None
            and retraso_enlace is None
            and programada - now_minutes <= UNCONFIRMED_LEAD_MINUTES
        )

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
                unconfirmed=sin_respaldo,
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
