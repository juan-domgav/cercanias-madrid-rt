"""Histórico de retrasos: del archivo de pasadas a una fila por (día, tren, parada).

El archivo (``archivo/<día>/<HHMMSS>.json``) guarda cada ~20 s lo que publica el
feed GTFS-RT de Renfe: ``trip``, ``delay``, ``next_stop``, ``status``… Aquí se
reduce, tren a tren, a lo que se vio en cada parada de su horario. Sin E/S: el
script ``scripts/construir_historico.py`` descarga, llama a esto y escribe Parquet.

Reglas no evidentes (todas medidas en el archivo, ver RB-010):

- **Día de servicio.** El prefijo del ``tripId`` (``1092X``) es el día de servicio:
  índice 01..99 que envuelve 99→01 y letra del día (ADR-012, RB-004). Pero pasada
  la medianoche Renfe re-etiqueta los trenes de la víspera con el día NUEVO y un
  ``delay`` de ``-86400 + retraso`` (00:00 del 22/10: ``1093J21875C7`` con
  -85260 es el ``1092X21875C7`` con +19 min). Y de noche aparecen viajes de
  madrugada del día siguiente, aún en cabecera, con ``+15..16 h``. Ningún tren va
  12 h tarde: un ``|delay| >= 12 h`` es siempre ese desfase de un día y se corrige.
- El feed puede **congelarse**: el 03/10 por la mañana repitió durante horas la
  misma foto de 28 trenes del viernes (``1073V``). Las pasadas idénticas repetidas
  durante ``CONGELADO_MIN`` o más se descartan (``pasadas_congeladas``).
- **Retraso antes de salir no es una medida (RB-009).** Mientras el tren está en
  su origen (``next_stop`` = origen y ``STOPPED_AT``/``INCOMING_AT``) Renfe publica
  0 o un contador de espera. Esas observaciones son *marcador*, nunca «en hora».
- **Retraso real en una parada** = el ``delay`` de la ÚLTIMA observación con
  ``next_stop`` = esa parada (fuera de cabecera), siempre que después se vea al
  tren con una parada posterior del horario (la ha pasado) o, en la última
  parada, ``STOPPED_AT`` en ella. Renfe mantiene ``next_stop`` = la parada hasta
  ~1 min después de salir (``IN_TRANSIT_TO``), así que ese último valor es el
  retraso a la SALIDA de la parada. En la última parada vale el de la primera
  ``STOPPED_AT`` (llegada): parado en destino Renfe sigue subiendo el retraso. Si
  el viaje desaparece en ``INCOMING_AT`` destino a su hora prevista (lo normal:
  Renfe lo retira al llegar) vale ese último valor. Ejemplo 21/10,
  21875 C7 en 70112 (prog. 22:11): ``STOPPED_AT`` 22:12 +60, sale 22:14:50 con +180.
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, tzinfo
from enum import StrEnum
from itertools import pairwise

from cercanias_domain.trip_id import parse_trip_id

#: Día de servicio de referencia: el 19/10/2026 (lunes) fue ``1090L`` en el feed.
#: Comprobado también el 08/09/2026 = ``1049M`` y el 29/10/2026 = ``1001J``.
_ANCLA_FECHA = date(2026, 10, 19)
_ANCLA_INDICE = 90
_CICLO = 99
_LETRAS = "LMXJVSD"
_NUCLEO_MADRID = "10"
_PREFIJO_ESPECIAL = "SPECIAL_"

#: Ningún tren va 12 h tarde ni adelantado: un ``delay`` así es el desfase de un
#: día del feed en torno a la medianoche.
DESFASE_DIA_S = 86_400
UMBRAL_DESFASE_S = 43_200
#: Corte para decidir a qué día pertenece una observación con desfase: antes de
#: mediodía, el tren es de la víspera; después, del día siguiente.
_MEDIODIA = time(12, 0)
#: Los especiales no llevan día de servicio en el id: hasta esta hora son de la
#: jornada anterior (igual que ``departures.OPERATING_DAY_ROLLOVER_HOUR``).
HORA_CAMBIO_JORNADA = 4

#: "Aún en el andén de origen" (RB-009, ``departures._AT_ORIGIN_STATUSES``).
ESTADOS_EN_ORIGEN = frozenset({"STOPPED_AT", "INCOMING_AT"})
SALIENDO = "IN_TRANSIT_TO"
PARADO = "STOPPED_AT"
LLEGANDO = "INCOMING_AT"
#: Ver ``_desaparece_al_llegar``: en el 21/10 la mediana fue +0,8 min.
MARGEN_LLEGADA = timedelta(minutes=2)


def servicio_madrid(fecha: date) -> str:
    """Id de servicio Madrid de un día: ``1090L`` el 19/10/2026."""
    dias = (fecha - _ANCLA_FECHA).days
    indice = (_ANCLA_INDICE - 1 + dias) % _CICLO + 1
    return f"{_NUCLEO_MADRID}{indice:02d}{_LETRAS[fecha.weekday()]}"


def fecha_de_servicio(servicio: str, cerca_de: date) -> date | None:
    """Día (a menos de ±49 días de ``cerca_de``) cuyo id de servicio es ``servicio``."""
    for delta in range(-(_CICLO // 2), _CICLO // 2 + 1):
        candidata = cerca_de + timedelta(days=delta)
        if servicio_madrid(candidata) == servicio:
            return candidata
    return None


@dataclass(frozen=True)
class Observacion:
    """Un tren en una pasada del archivo, ya en hora local de Madrid."""

    ts: datetime
    delay: int | None
    next_stop: str | None
    status: str | None


@dataclass(frozen=True)
class ObservacionNormalizada:
    fecha_servicio: date
    trip_id: str
    obs: Observacion


def normalizar(trip_id: str, obs: Observacion) -> ObservacionNormalizada | None:
    """Asigna día de servicio y ``tripId`` canónico; corrige el desfase de un día.

    Devuelve ``None`` si el id no es de Madrid ni especial de Madrid.
    """
    local = obs.ts
    if trip_id.startswith(_PREFIJO_ESPECIAL):
        if not trip_id.startswith(f"{_PREFIJO_ESPECIAL}{_NUCLEO_MADRID}_"):
            return None
        dia = local.date() - timedelta(days=1) if local.hour < HORA_CAMBIO_JORNADA else local.date()
        return ObservacionNormalizada(dia, trip_id, obs)
    servicio = trip_id[:5]
    if not servicio.startswith(_NUCLEO_MADRID):
        return None
    etiqueta = fecha_de_servicio(servicio, local.date())
    if etiqueta is None:
        return None
    delay = obs.delay
    dia = etiqueta
    if delay is not None and delay <= -UMBRAL_DESFASE_S:
        delay += DESFASE_DIA_S
        if local.date() == etiqueta and local.time() < _MEDIODIA:
            dia = etiqueta - timedelta(days=1)
    elif delay is not None and delay >= UMBRAL_DESFASE_S:
        delay -= DESFASE_DIA_S
        if local.date() == etiqueta and local.time() >= _MEDIODIA:
            dia = etiqueta + timedelta(days=1)
    canonico = servicio_madrid(dia) + trip_id[5:]
    return ObservacionNormalizada(dia, canonico, Observacion(obs.ts, delay, obs.next_stop, obs.status))


@dataclass(frozen=True)
class ParadaProgramada:
    seq: int
    stop_id: str
    #: ``HH:MM:SS`` del GTFS, que puede pasar de 24 (``24:14:00``).
    hora: str


class EstadoMedida(StrEnum):
    #: Retraso de la última observación en la parada y el tren la ha pasado.
    MEDIDO = "medido"
    #: Sólo observaciones con el tren aún en su origen: 0 o contador de Renfe (RB-009).
    MARCADOR = "marcador"
    #: Visto hacia la parada pero no consta que la pasara (dejó de verse, hueco…).
    SIN_CONFIRMAR = "sin_confirmar"
    #: Ninguna observación con ``next_stop`` = la parada.
    NO_OBSERVADO = "no_observado"


@dataclass
class FilaParada:
    """Una fila del histórico."""

    fecha_servicio: date
    trip_id: str
    stop_id: str
    seq: int | None
    programada: datetime | None
    es_origen: bool
    es_destino: bool
    tren_visto: bool
    salio: bool
    estado_medida: EstadoMedida
    retraso_s: int | None = None
    retraso_primero_s: int | None = None
    primero_marcador: bool | None = None
    primero_ts: datetime | None = None
    ultimo_ts: datetime | None = None
    ultimo_estado: str | None = None
    n_obs: int = 0
    llegada_obs: datetime | None = None
    #: ``llegada_obs`` - hora programada (Renfe sólo publica una hora por parada).
    retraso_llegada_obs_s: int | None = None
    salida_obs: datetime | None = None
    retraso_salida_obs_s: int | None = None

    @property
    def medido(self) -> bool:
        return self.estado_medida is EstadoMedida.MEDIDO


def hora_programada(fecha: date, hhmmss: str, zona: tzinfo) -> datetime:
    """``24:14:00`` del día D = 00:14 del día D+1 (hora de pared en ``zona``).

    GTFS mide desde el mediodía menos 12 h; en los cambios de hora de Madrid
    (madrugada, sin servicio) la diferencia no afecta a ningún tren.
    """
    h, m, s = (int(x) for x in hhmmss.strip().split(":"))
    dias, h = divmod(h, 24)
    pared = datetime.combine(fecha + timedelta(days=dias), time(h, m, s))
    return pared.replace(tzinfo=zona)


def _es_marcador(o: Observacion, origen: str | None) -> bool:
    return origen is not None and o.next_stop == origen and o.status in ESTADOS_EN_ORIGEN


def _salida_observada(obs: Sequence[Observacion], parada: str) -> datetime | None:
    """Primera transición ``STOPPED_AT`` -> ``IN_TRANSIT_TO`` con ``next_stop`` = parada."""
    previo: Observacion | None = None
    for o in obs:
        if (
            previo is not None
            and o.next_stop == parada
            and o.status == SALIENDO
            and previo.next_stop == parada
            and previo.status == PARADO
        ):
            return o.ts
        previo = o
    return None


def _desaparece_al_llegar(obs: Sequence[Observacion], k: int, programada: datetime | None) -> bool:
    """El viaje deja de publicarse llegando a destino, cuando tocaba llegar.

    Renfe suele retirar el viaje al entrar en la última estación sin pasar por
    ``STOPPED_AT``: la última observación es ``INCOMING_AT`` destino. Se acepta
    como llegada si es la última del viaje y no es anterior a programada +
    retraso - ``MARGEN_LLEGADA`` (no se cortó a medio camino por un hueco).
    """
    o = obs[k]
    if k != len(obs) - 1 or o.status != LLEGANDO or programada is None or o.delay is None:
        return False
    return o.ts >= programada + timedelta(seconds=o.delay) - MARGEN_LLEGADA


def filas_viaje(
    fecha: date,
    trip_id: str,
    obs: Sequence[Observacion],
    horario: Sequence[ParadaProgramada] | None,
    zona: tzinfo,
) -> list[FilaParada]:
    """Filas de un viaje: una por parada del horario (o por parada vista si no lo hay).

    ``obs`` debe venir ordenado por ``ts``. Sin horario (especiales, viajes que
    no están en el GTFS) las paradas son las vistas en el feed, en orden de
    aparición, con ``seq`` = ``None``.
    """
    obs = sorted(obs, key=lambda o: o.ts)
    if horario:
        ordenadas = sorted(horario, key=lambda p: p.seq)
        paradas: list[tuple[int | None, str, str | None]] = [(p.seq, p.stop_id, p.hora) for p in ordenadas]
        origen: str | None = ordenadas[0].stop_id
    else:
        vistas: list[str] = []
        for o in obs:
            if o.next_stop and o.next_stop not in vistas:
                vistas.append(o.next_stop)
        paradas = [(None, s, None) for s in vistas]
        primera = obs[0] if obs else None
        # Sin horario sólo se sabe que es su origen si se le ve primero PARADO
        # ahí (Renfe publica el viaje ~30 min antes de salir, en el andén).
        origen = primera.next_stop if primera is not None and primera.status == PARADO else None
    orden = {stop: i for i, (_, stop, _) in enumerate(paradas)}
    salio = any(not _es_marcador(o, origen) for o in obs)
    filas: list[FilaParada] = []
    ultima = len(paradas) - 1
    for i, (seq, stop, hora) in enumerate(paradas):
        programada = hora_programada(fecha, hora, zona) if hora else None
        aqui = [(k, o) for k, o in enumerate(obs) if o.next_stop == stop]
        fila = FilaParada(
            fecha_servicio=fecha,
            trip_id=trip_id,
            stop_id=stop,
            seq=seq,
            programada=programada,
            es_origen=stop == origen and i == 0,
            es_destino=i == ultima,
            tren_visto=bool(obs),
            salio=salio,
            estado_medida=EstadoMedida.NO_OBSERVADO,
            n_obs=len(aqui),
        )
        if aqui:
            primera_obs = aqui[0][1]
            fila.retraso_primero_s = primera_obs.delay
            fila.primero_marcador = _es_marcador(primera_obs, origen)
            fila.primero_ts = primera_obs.ts
            validas = [(k, o) for k, o in aqui if not _es_marcador(o, origen) and o.delay is not None]
            if not validas:
                fila.estado_medida = (
                    EstadoMedida.MARCADOR
                    if all(_es_marcador(o, origen) for _, o in aqui)
                    else EstadoMedida.SIN_CONFIRMAR
                )
            else:
                k, ultima_obs = validas[-1]
                fila.ultimo_ts = ultima_obs.ts
                fila.ultimo_estado = ultima_obs.status
                en_anden = [o for _, o in validas if o.status == PARADO]
                if i == ultima and en_anden:
                    # En destino el tren se queda STOPPED_AT y Renfe sigue
                    # subiendo el retraso mientras espera: vale el de la llegada.
                    fila.retraso_s = en_anden[0].delay
                    fila.estado_medida = EstadoMedida.MEDIDO
                else:
                    fila.retraso_s = ultima_obs.delay
                    pasada = any(orden.get(o.next_stop or "", -1) > i for o in obs[k + 1 :])
                    llega = i == ultima and _desaparece_al_llegar(obs, k, programada)
                    fila.estado_medida = (
                        EstadoMedida.MEDIDO if pasada or llega else EstadoMedida.SIN_CONFIRMAR
                    )
            fila.llegada_obs = next(
                (o.ts for _, o in aqui if o.status == PARADO and not _es_marcador(o, origen)), None
            )
            if fila.llegada_obs is not None and programada is not None:
                fila.retraso_llegada_obs_s = int((fila.llegada_obs - programada).total_seconds())
            fila.salida_obs = _salida_observada(obs, stop)
            if fila.salida_obs is not None and programada is not None and i != ultima:
                fila.retraso_salida_obs_s = int((fila.salida_obs - programada).total_seconds())
        filas.append(fila)
    return filas


def huecos(marcas: Iterable[datetime], umbral: timedelta) -> list[tuple[datetime, datetime]]:
    """Pares (antes, después) de pasadas consecutivas separadas más de ``umbral``."""
    ordenadas = sorted(marcas)
    return [(a, b) for a, b in pairwise(ordenadas) if b - a > umbral]


#: Una foto idéntica repetida tanto tiempo es un feed congelado, no trenes quietos.
CONGELADO_MIN = timedelta(minutes=10)


def pasadas_congeladas(
    pasadas: Sequence[tuple[datetime, Hashable | None]], minimo: timedelta = CONGELADO_MIN
) -> list[bool]:
    """Qué pasadas (ordenadas por hora) son repeticiones de un feed congelado.

    ``pasadas`` son ``(ts, firma)``; la firma es el contenido completo de la pasada,
    o ``None`` si no debe considerarse (vacía o con muy pocos trenes). Se marca cada
    repetición de una racha de firmas idénticas que dura ``minimo`` o más; la primera
    pasada de la racha se conserva porque sí era una observación real.
    """
    marcadas = [False] * len(pasadas)
    i = 0
    while i < len(pasadas):
        j = i
        while j + 1 < len(pasadas) and pasadas[j + 1][1] == pasadas[i][1]:
            j += 1
        if pasadas[i][1] is not None and pasadas[j][0] - pasadas[i][0] >= minimo:
            for k in range(i + 1, j + 1):
                marcadas[k] = True
        i = j + 1
    return marcadas


def familia(trip_id: str) -> str | None:
    """``C4`` para ``1092X20531C4b``; ``None`` si el id no se reconoce."""
    parsed = parse_trip_id(trip_id)
    return parsed.route_group if parsed else None
