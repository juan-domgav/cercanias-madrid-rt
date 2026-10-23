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
- **Los trenes especiales no tienen horario.** Los ``SPECIAL_10_*`` (Anexo A.2)
  pasan el filtro de núcleo pero no existen en el GTFS estático: un tablón que
  sólo recorre el horario no los ve nunca, aunque estén parados en el andén.
  Se añaden aparte, desde el feed, en su parada actual o próxima
  (:func:`live_special_departures`). Ver RB-006.
- **Renfe no publica la vuelta hasta poco antes de salir.** Un viaje que empieza
  en cabecera dando la vuelta a otro tren no aparece en el feed hasta ~30 min
  antes de su salida (observado el 21/10/2026 en Colmenar Viejo y Parla). Si el
  entrante sí está localizado, se ESTIMA la salida (:func:`estimate_from_inbound`)
  y se etiqueta como estimada, nunca como dato en vivo. Ver RB-007.
"""

from __future__ import annotations

from dataclasses import dataclass

from cercanias_domain.trip_id import parse_trip_id

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

#: Hasta cuánto después de su hora programada puede seguir en el tablón un tren
#: con retraso (en vivo, cota de cabecera o estimado). Sólo acota el recorrido.
MAX_LINGER_MINUTES = 180

#: Materialidad mínima. Renfe considera puntual un Cercanías con menos de tres
#: minutos, así que por debajo de eso avisar es ruido AUNQUE la maniobra sea 0 y
#: el andén esté técnicamente vacío. Se aplica siempre, no sólo cuando falta el
#: dato de maniobra: un aviso que no cambia ninguna decisión enseña a ignorar los
#: que sí la cambian.
INBOUND_WARNING_FLOOR_MINUTES = 3

#: Origen del retraso mostrado (``Departure.delay_source``).
DELAY_SOURCE_LIVE = "live"
DELAY_SOURCE_INBOUND_ESTIMATE = "inbound_estimate"

#: Renfe publica cada viaje en GTFS-RT unos 30 min antes de su salida de
#: CABECERA (archivo del 21/10/2026, n=850: p10 29, mediana 30, p90 30 min).
#: Un viaje sin datos cuya salida de origen queda a MAS de esto no es una
#: anomalia: "Renfe aun no lo publica". Dentro de la ventana, faltar si lo es.
#: Configurable en la ingesta con ``PUBLISH_WINDOW_MINUTES``. Ver RB-007.
PUBLISH_WINDOW_MINUTES = 30

#: ``Departure.realtime_status``, por orden de precedencia.
REALTIME_LIVE = "live"
#: En el feed pero AÚN en su estación de origen (RB-009): el ``delay`` que publica
#: Renfe antes de la salida no es una medida.
REALTIME_PENDING_DEPARTURE = "pending_departure"
REALTIME_INBOUND_ESTIMATE = "inbound_estimate"
REALTIME_NOT_PUBLISHED = "not_published"
REALTIME_NO_DATA = "no_data"


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
    #: Estación de ORIGEN del viaje (``seq == 1``), en todas sus paradas. Sirve
    #: para saber si el tren sigue en cabecera sin haber salido (RB-009).
    origin_stop_id: str | None = None
    #: El enlace se eligió entre candidatos casi empatados: no se sabe qué tren
    #: físico hará el servicio, así que NO se estima nada desde el entrante.
    inbound_ambiguous: bool = False
    #: Hora de salida del viaje en su ORIGEN (``seq == 1``). La ventana de
    #: publicacion de Renfe se mide desde ahi, no desde esta estacion.
    origin_departure_hhmmss: str | None = None


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
    #: ``scheduleRelationship = CANCELED``. El feed SÍ publica las supresiones.
    cancelled: bool = False
    #: Paradas marcadas ``SKIPPED``: el tren no las efectuará. Es la señal de un
    #: recorrido acortado —un tren que da la vuelta antes de su destino— que el
    #: horario estático no puede expresar.
    skips_stops: bool = False
    #: ``currentStatus`` del feed (``STOPPED_AT``, ``INCOMING_AT``,
    #: ``IN_TRANSIT_TO``). Sólo lo usan los especiales: sin horario, es lo único
    #: que dice si el tren ya está en el andén o todavía llegando.
    status: str | None = None


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
    #: La hora se echa encima y no hay ni rastro del tren ni de su enlace. NO
    #: significa suprimido: para eso está :attr:`cancelled`, que sí es un dato
    #: publicado por el feed.
    unconfirmed: bool = False
    #: El propio servicio está suprimido (``CANCELED``). No es una sospecha.
    cancelled: bool = False
    #: El tren que debía formar este servicio está suprimido. Salvo que Renfe
    #: cubra con otro material, este tren no va a salir.
    inbound_cancelled: bool = False
    #: El tren no efectuará todas sus paradas: puede no llegar al destino que
    #: anuncia el horario. Observado en Tres Cantos, donde un tren terminó su
    #: recorrido sin continuar a Colmenar.
    shortened: bool = False
    #: Tren especial (``SPECIAL_10_*``) sin horario programado: la hora mostrada
    #: es la de AHORA, no una hora de horario, y no hay retraso que calcular.
    special: bool = False
    #: ``currentStatus`` del feed, sólo para especiales (ver :class:`LiveTrain`).
    status: str | None = None
    #: Especial en una estación que para su línea es sólo cabecera: puede estar
    #: llegando al final de su recorrido, no saliendo.
    at_terminus: bool = False
    #: Retraso ESTIMADO desde el enlace entrante cuando el propio viaje aún no
    #: está en el feed (RB-007). Ya incluido en ``expected_hhmm``. No es un dato
    #: en vivo: ``delay_minutes`` sigue en ``None`` y ``located`` en ``False``.
    estimated_delay_minutes: int | None = None
    #: Sin dato en vivo ni estimado, y su salida de origen esta a mas de
    #: ``PUBLISH_WINDOW_MINUTES``: Renfe aun no lo publica. No es una anomalia.
    not_published: bool = False
    #: El tren está en el feed pero sigue en su estación de origen sin haber
    #: salido (RB-009). Antes de su hora de salida ``delay_minutes`` es ``None``:
    #: Renfe publica 0 (o el retraso que arrastra la unidad) y no es una medida.
    #: Pasada la hora, ``delay_minutes`` es una COTA INFERIOR (lo que ya lleva).
    pending_departure: bool = False

    @property
    def delay_source(self) -> str | None:
        """``"live"``, ``"inbound_estimate"`` o ``None`` (sin dato)."""
        if self.delay_minutes is not None:
            return DELAY_SOURCE_LIVE
        if self.estimated_delay_minutes is not None:
            return DELAY_SOURCE_INBOUND_ESTIMATE
        return None

    @property
    def realtime_status(self) -> str:
        """``live`` > ``pending_departure`` > ``inbound_estimate`` > ``not_published`` > ``no_data``."""
        if self.pending_departure:
            return REALTIME_PENDING_DEPARTURE
        if self.located:
            return REALTIME_LIVE
        if self.estimated_delay_minutes is not None:
            return REALTIME_INBOUND_ESTIMATE
        if self.not_published:
            return REALTIME_NOT_PUBLISHED
        return REALTIME_NO_DATA

    @property
    def at_risk(self) -> bool:
        """¿Hay motivo fundado para dudar de la hora prevista?"""
        return bool(self.inbound_delay_minutes)

    @property
    def shows_on_time(self) -> bool:
        """¿Puede afirmarse "en hora"?

        Sí cuando el propio viaje está en el feed con ``delay <= 0`` (RB-008: el
        dato en vivo manda). ``at_risk`` sólo puede ser cierto para un viaje que
        NO está en el feed, así que aquí sólo frena lo que no tiene dato propio.
        """
        return (
            self.delay_minutes is not None
            and self.delay_minutes <= 0
            and not self.at_risk
            and not self.pending_departure
            and not self.cancelled
            and not self.inbound_cancelled
        )

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
    aparecen como tres tarjetas idénticas y parecen el mismo tren repetido.

    ``SPECIAL_10_91887C4B`` -> ``91887``. El recorte fijo de 5 caracteres del
    formato estándar daba ``"10"`` (el prefijo de núcleo) para todo especial.
    """
    parsed = parse_trip_id(trip_id)
    if parsed is not None and parsed.is_special:
        return parsed.train_number
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
    #: Estimar la salida desde el enlace entrante (RB-007). Desactivado por
    #: defecto: quien llama lo activa SOLO con un snapshot fresco (guard de
    #: PR #7). Con datos conservados, proyectar sería inventar.
    estimate_from_inbound: bool = False
    #: Familias de línea (``C4``, ``C8``…) cuyo emparejamiento de cabecera no
    #: está validado: ni se estima (RB-007) ni se avisa de "el tren que hace este
    #: servicio viene con +N" (RB-008). Configuración, no código.
    estimate_excluded_routes: frozenset[str] = frozenset()
    #: Ventana de publicacion de Renfe. ``None`` = no afirmar "no publicado"
    #: (snapshot viejo o degradado: no sabemos que hay en el feed).
    publish_window_minutes: int | None = None
    #: Familias de línea en las que, con el tren AÚN en cabecera y antes de su
    #: hora, se estima la salida desde el entrante (RB-009). Lista PERMITIDA, vacía
    #: por defecto: la validación del 19-22/10/2026 no alcanzó el umbral (>= 80 %
    #: a +-3 min con 5 y 10 min de antelación, n >= 30) en ninguna línea.
    origin_estimate_routes: frozenset[str] = frozenset()


#: Estados del feed que significan "todavía en el andén de origen". Tras salir,
#: Renfe pasa a ``IN_TRANSIT_TO`` con ``next_stop`` = origen durante unos minutos
#: (archivo 19-22/10: 4121 de 4126 viajes empiezan ``STOPPED_AT`` en cabecera y
#: la salida es la transición a ``IN_TRANSIT_TO``).
_AT_ORIGIN_STATUSES = frozenset({"STOPPED_AT", "INCOMING_AT"})


def _still_at_origin(stop: ScheduledStop, tren: LiveTrain | None) -> bool:
    """¿Está el tren en el feed pero aún sin salir de su estación de origen?"""
    return (
        tren is not None
        and not tren.cancelled
        and stop.origin_stop_id is not None
        and tren.next_stop_id == stop.origin_stop_id
        and tren.status in _AT_ORIGIN_STATUSES
    )


def estimate_from_inbound(inbound_delay_minutes: int, scheduled_layover_minutes: int) -> int:
    """Retraso proyectado de la vuelta: ``max(0, retraso - holgura)``.

    ``holgura = maniobra programada - MIN_PHYSICAL_TURNAROUND_MINUTES``: lo que
    el entrante puede llegar tarde sin que la vuelta se resienta. Un adelanto
    jamás se propaga: el tren no sale antes de su hora.
    """
    holgura = scheduled_layover_minutes - MIN_PHYSICAL_TURNAROUND_MINUTES
    return max(0, inbound_delay_minutes - holgura)


def _route_family(stop: ScheduledStop) -> str:
    parsed = parse_trip_id(stop.trip_id)
    return parsed.route_group if parsed else stop.route


def _estimate_allowed(stop: ScheduledStop, propio: LiveTrain | None, query: DepartureQuery) -> bool:
    """¿Se puede estimar desde el entrante? Sin el viaje en el feed, salvo líneas
    excluidas (RB-007); con el tren aún en cabecera, sólo líneas validadas (RB-009)."""
    if not query.estimate_from_inbound or not stop.inbound_trip_id or stop.inbound_ambiguous:
        return False
    familia = _route_family(stop)
    if propio is None:
        return familia not in query.estimate_excluded_routes
    return _still_at_origin(stop, propio) and familia in query.origin_estimate_routes


def _inbound_estimate(
    stop: ScheduledStop,
    live: dict[str, LiveTrain],
    propio: LiveTrain | None,
    query: DepartureQuery,
) -> int | None:
    """Retraso estimado desde el entrante, o ``None`` si no procede.

    Vale en TODAS las paradas del viaje, no sólo en cabecera: mientras el viaje
    no está en el feed, su retraso en Tres Cantos es el que arrastre desde
    Colmenar. En cuanto aparece, su dato manda (``propio``).
    """
    if not _estimate_allowed(stop, propio, query) or stop.turnaround_slack_minutes is None:
        return None
    entrante = live.get(stop.inbound_trip_id) if stop.inbound_trip_id else None
    if entrante is None or entrante.cancelled or entrante.delay_seconds is None:
        return None
    return estimate_from_inbound(
        round(entrante.delay_seconds / 60), stop.turnaround_slack_minutes
    )


def _inbound_warning(
    stop: ScheduledStop,
    live: dict[str, LiveTrain],
    propio: LiveTrain | None,
    query: DepartureQuery,
) -> int | None:
    """Minutos de retraso del enlace entrante, si procede avisar.

    Todas las condiciones son necesarias. Cada una corresponde a una forma
    concreta de mentirle a alguien que está en un andén.
    """
    # 1. Sólo en el origen del viaje. En una parada intermedia el tren ya circula
    #    y el enlace de cabecera no dice nada sobre él.
    if not stop.is_origin or not stop.inbound_trip_id:
        return None

    # 2. El dato EN VIVO del propio viaje manda, sea el que sea (RB-008). Antes un
    #    ``delay`` de 0 se tomaba por "marcador de posición" y se avisaba igual:
    #    el 22/10/2026 a las 08:36 el 20428 estaba en Colmenar, andén 3, +0, y
    #    salió en hora (Renfe lo confirma), pero el bot decía "sin confirmar" y
    #    "viene con +17" porque el 20427 —que NO lo formaba: en Colmenar Renfe
    #    cambia de material— iba +17 por Tres Cantos. Con el viaje en el feed,
    #    un aviso desde el entrante sólo puede contradecir el dato en vivo.
    if propio is not None and not (
        _still_at_origin(stop, propio) and _route_family(stop) in query.origin_estimate_routes
    ):
        return None

    # 3. Sólo con un emparejamiento fiable. Mismo criterio que la estimación
    #    (RB-007): empate entre candidatos o familia de línea no validada
    #    (C4b acierta el 47 %) = no se sabe qué tren físico hace el servicio.
    if stop.inbound_ambiguous or _route_family(stop) in query.estimate_excluded_routes:
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
        if programada > now_minutes + window_minutes:
            continue
        # El margen se mide sobre la hora PREVISTA, no la programada (RB-009): un
        # tren que va +10 (o que sigue en el andén de cabecera pasada su hora) no
        # puede desaparecer del tablón 3 minutos después de su hora de horario.
        # Corte barato primero: ningún retraso publicado pasa de unas horas.
        if programada < now_minutes - grace_minutes - MAX_LINGER_MINUTES:
            continue

        tren = live.get(stop.trip_id)
        retraso = None
        if tren is not None and tren.delay_seconds is not None:
            retraso = round(tren.delay_seconds / 60)

        # RB-009: con el tren aún en cabecera, el delay del feed no es una medida.
        # Antes de su hora: sin retraso (ni 0 ni el que arrastre la unidad). Ya
        # pasada: lo que lleva esperando es una cota inferior cierta.
        en_cabecera = _still_at_origin(stop, tren)
        if en_cabecera:
            salida_origen = (
                parse_hhmm_to_minutes(stop.origin_departure_hhmmss)
                if stop.origin_departure_hhmmss
                else programada
            )
            esperando = now_minutes - salida_origen
            retraso = max(retraso or 0, esperando) if esperando > 0 else None

        # El aviso del enlace SOLO aplica mientras el viaje no esté en el feed: en
        # cuanto aparece, su dato manda y el del enlace sobra (RB-008).
        retraso_enlace = _inbound_warning(stop, live, tren, query)
        estimado = _inbound_estimate(stop, live, tren, query) if retraso is None else None
        desfase = retraso if retraso is not None else (estimado or 0)
        if programada + max(desfase, 0) < now_minutes - grace_minutes:
            continue

        entrante_vivo = live.get(stop.inbound_trip_id) if stop.inbound_trip_id else None
        suprimido = bool(tren and tren.cancelled)
        # Con el propio viaje en el feed, la supresión del entrante queda
        # desmentida: ya hay material haciendo el servicio (RB-008).
        entrante_suprimido = bool(
            tren is None and stop.is_origin and entrante_vivo and entrante_vivo.cancelled
        )

        # Un tren que termina su recorrido desaparece del feed con normalidad: es
        # el caso NORMAL de una maniobra, no una señal de supresión. Por eso la
        # ausencia del entrante no genera aviso. Lo que sí se puede afirmar es que
        # no hay nada que respalde esta hora.
        origen = stop.origin_departure_hhmmss
        no_publicado = (
            tren is None
            and estimado is None
            and query.publish_window_minutes is not None
            and origen is not None
            and parse_hhmm_to_minutes(origen) - now_minutes > query.publish_window_minutes
        )
        sin_respaldo = (
            tren is None
            and retraso_enlace is None
            and estimado is None
            and not entrante_suprimido
            and programada - now_minutes <= UNCONFIRMED_LEAD_MINUTES
        )

        salidas.append(
            Departure(
                scheduled_hhmm=format_minutes(programada),
                expected_hhmm=format_minutes(programada + desfase),
                route=stop.route,
                destination=stop.destination,
                train_number=train_number(stop.trip_id),
                delay_minutes=retraso,
                platform=tren.platform if tren else None,
                minutes_until=programada + desfase - now_minutes,
                located=tren is not None,
                inbound_delay_minutes=retraso_enlace,
                unconfirmed=sin_respaldo,
                cancelled=suprimido,
                inbound_cancelled=entrante_suprimido,
                shortened=bool(tren and tren.skips_stops),
                estimated_delay_minutes=estimado,
                not_published=no_publicado,
                pending_departure=en_cabecera,
            )
        )

    salidas.sort(key=lambda d: parse_hhmm_to_minutes(d.expected_hhmm))
    return tuple(salidas[:limit])


def _route_groups_ending_here(scheduled: list[ScheduledStop]) -> frozenset[str]:
    """Familias de línea para las que esta estación es SÓLO cabecera.

    Cabecera = ningún viaje programado de esa familia pasa de largo: todos
    empiezan o terminan aquí (Colmenar Viejo para el C4). Un especial cuya
    parada es una cabecera puede estar LLEGANDO al final de su recorrido; el
    feed no permite distinguirlo de uno que va a salir. Una estación de paso con
    algún viaje acortado (Tres Cantos) no cuenta: allí el C4 también pasa.
    """
    cabecera: set[str] = set()
    de_paso: set[str] = set()
    for stop in scheduled:
        parsed = parse_trip_id(stop.trip_id)
        grupo = parsed.route_group if parsed else stop.route
        (cabecera if stop.terminates_here or stop.is_origin else de_paso).add(grupo)
    return frozenset(cabecera - de_paso)


def live_special_departures(
    live: dict[str, LiveTrain],
    stop_id: str,
    now_minutes: int,
    *,
    observed_minutes: int | None = None,
    scheduled: list[ScheduledStop] | None = None,
) -> tuple[Departure, ...]:
    """Trenes especiales (``SPECIAL_10_*``) cuya parada actual o próxima es ``stop_id``.

    No están en el GTFS estático, así que :func:`next_departures` —que recorre el
    horario y sólo después busca el tren en vivo por ``trip_id``— no puede
    encontrarlos nunca. Aquí se parte del feed: el tren está localizado, así que
    es ``located`` y jamás ``unconfirmed``.

    Sin horario no hay hora programada ni retraso: la hora es la de la
    OBSERVACIÓN (``observed_minutes``, por defecto ``now_minutes``) y el retraso
    queda en ``None`` (no se inventa un "en hora"). Así un snapshot conservado no
    rejuvenece al especial en cada reconstrucción. El destino no se conoce: el
    feed no lo trae y el sentido no se puede afirmar sin topología.

    Si ``scheduled`` indica que la estación es sólo cabecera para la familia del
    especial, se marca ``at_terminus``: puede ser una llegada, y se dice en vez
    de presentarla como salida segura (ni se oculta: también puede salir).
    """
    visto = now_minutes if observed_minutes is None else observed_minutes
    finales = _route_groups_ending_here(scheduled) if scheduled else frozenset()
    salidas: list[Departure] = []
    for tren in live.values():
        if tren.next_stop_id != stop_id:
            continue
        parsed = parse_trip_id(tren.trip_id)
        if parsed is None or not parsed.is_special:
            continue
        salidas.append(
            Departure(
                scheduled_hhmm=format_minutes(visto),
                expected_hhmm=format_minutes(visto),
                route=parsed.route_canonical,
                destination=None,
                train_number=parsed.train_number,
                delay_minutes=None,
                platform=tren.platform,
                minutes_until=visto - now_minutes,
                located=True,
                unconfirmed=False,
                cancelled=tren.cancelled,
                shortened=tren.skips_stops,
                special=True,
                status=tren.status,
                at_terminus=parsed.route_group in finales,
            )
        )
    salidas.sort(key=lambda d: d.train_number)
    return tuple(salidas)


def board_departures(
    scheduled: list[ScheduledStop],
    live: dict[str, LiveTrain],
    query: DepartureQuery,
    stop_id: str,
    *,
    specials_observed_minutes: int | None = None,
) -> tuple[Departure, ...]:
    """Tablón completo de una estación: horario casado con el feed + especiales.

    Sin especiales en la estación devuelve exactamente :func:`next_departures`.
    Con ellos, los especiales tienen prioridad (están en el andén o llegando: es
    el dato más inmediato del tablón) y el resto del cupo lo llena el horario;
    nunca se pasa de ``query.limit``.

    Con ``query.destination`` no se incluye ningún especial: su destino es
    desconocido y el dominio no afirma que vaya hacia donde se ha pedido. (La web
    decide aparte mostrarlos en cualquier sentido, etiquetados.)

    El tren programado al que un especial sustituye, si lo hay, sigue saliendo
    aparte (normalmente ``unconfirmed``): el feed no los relaciona y no hay forma
    segura de deduplicarlos. Ver RB-006.
    """
    programadas = next_departures(scheduled, live, query)
    if query.destination:
        return programadas
    especiales = live_special_departures(
        live, stop_id, query.now_minutes,
        observed_minutes=specials_observed_minutes, scheduled=scheduled,
    )[: query.limit]
    if not especiales:
        return programadas
    hueco = max(query.limit - len(especiales), 0)
    combinadas = [*especiales, *programadas[:hueco]]
    combinadas.sort(key=lambda d: d.minutes_until)
    return tuple(combinadas)


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
