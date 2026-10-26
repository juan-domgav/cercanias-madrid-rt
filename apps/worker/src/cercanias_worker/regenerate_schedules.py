"""Regenera los horarios diarios de Cercanías Madrid desde el GTFS estático."""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path

from cercanias_connectors_renfe.gtfs_static_downloader import (
    GtfsStaticUnavailableError,
    download_gtfs_static,
)
from cercanias_storage.publishers import publisher_from_env

GTFS_STATIC_DIR = Path("data/gtfs_static")
SCHEDULES_PREFIX = "horarios"
SCHEDULE_CACHE_CONTROL = "public, max-age=3600"
MADRID_PREFIX = "10"
MAX_TURNAROUND_MINUTES = 40
#: Dos candidatos (llegadas para una salida, o salidas para una llegada) a esta
#: distancia o menos se consideran empatados: el enlace se publica marcado
#: ``amb`` y el tablon no estima desde el (RB-007).
AMBIGUITY_TIE_MINUTES = 2
#: Cabeceras donde un tren cambia de familia de linea al dar la vuelta, y SOLO
#: entre esas familias. Fuera de aqui el enlace exige la misma familia: en un
#: nudo como Chamartin (C1, C2, C3, C8, C10) cruzar familias es adivinar, y el
#: emparejamiento sin restriccion asignaba alli 280 vueltas C1<->C3<->C2<->C10
#: que el feed no respalda. Ver docs/runbooks/RB-007 (tabla de validacion).
INTERLINE_TERMINI: dict[str, frozenset[str]] = {
    "12006": frozenset({"C8", "C9"}),  # Cercedilla: el C8 vuelve como C9 a Cotos
}
LINE_VARIANT_MIN_LENGTH = 3
#: Maniobra MINIMA (minutos) entre la llegada de un tren a una cabecera y la
#: salida que puede hacer con ese mismo material (RB-012). Fuera de la tabla vale
#: 0: la llegada sin consumir mas proxima a la salida, el comportamiento de main.
#:
#: Donde el horario deja menos que esto entre una llegada y la salida siguiente,
#: esa llegada no forma esa salida sino una posterior (en Mostoles-El Soto, con 3
#: min de horario, el 19545 de las 07:42 no forma el 20754 de las 07:45 sino el
#: 19564 de las 07:50, archivo del 21/10/2026).
#:
#: VACIA A PROPOSITO: el backtest de 8 dias laborables de octubre (RB-012) no da
#: muestra suficiente para fijar ningun valor. Ninguno esta aprobado. Se activa,
#: si acaso, con ``MIN_TURNAROUND_BY_TERMINUS`` en el job (ver RB-012), tras 2-3
#: semanas mas de datos con fines de semana y supresiones.
DEFAULT_MIN_TURNAROUND_BY_TERMINUS: dict[str, int] = {}
#: Separadores de ``MIN_TURNAROUND_BY_TERMINUS``: ``"17005:5;70200:5"``. Ni coma
#: ni ``=``: ``gcloud --update-env-vars`` parte por comas y no hace falta el
#: escape ``^@^``. Cualquier otra forma invalida la variable ENTERA.
MIN_TURNAROUND_ENTRY_SEPARATOR = ";"
MIN_TURNAROUND_VALUE_SEPARATOR = ":"
MIN_TURNAROUND_ENV = "MIN_TURNAROUND_BY_TERMINUS"
#: Renfe publica ~30 dias por delante. Menos de esto significa que el GTFS viene
#: recortado o que el filtro de Madrid ha dejado de casar: hay que enterarse
#: antes de que los tablones se queden vacios, no despues (los horarios
#: caducaron sin aviso a finales de agosto de 2026).
MIN_SERVICE_DAYS = int(os.environ.get("MIN_SERVICE_DAYS", "25"))
EXIT_TOO_FEW_DAYS = 2


@dataclass(frozen=True)
class StopTime:
    """Una parada de un viaje, conservada tras el único recorrido de stop_times."""

    stop_id: str
    arrival_time: str
    departure_time: str
    sequence: int


@dataclass
class Trip:
    """Los datos mínimos necesarios para construir un viaje del horario."""

    trip_id: str
    service_id: str
    route: str
    stops: list[StopTime] = field(default_factory=list)
    #: ``block_id`` del GTFS. Renfe lo rellena con el propio numero de tren (un
    #: viaje por bloque), asi que hoy no aporta nada; solo se usa si agrupa
    #: dos o mas viajes.
    block_id: str = ""

    @property
    def origin(self) -> StopTime:
        return min(self.stops, key=lambda stop: stop.sequence)

    @property
    def terminal(self) -> StopTime:
        return max(self.stops, key=lambda stop: stop.sequence)


@dataclass(frozen=True)
class Turnaround:
    inbound_trip_id: str
    slack_minutes: int
    #: Habia otro candidato empatado (``AMBIGUITY_TIE_MINUTES``): no se sabe que
    #: tren fisico hace el servicio.
    ambiguous: bool = False


def _gtfs_rows(path: Path) -> Iterator[dict[str, str]]:
    """Filas de un CSV del GTFS con claves Y valores ya limpios.

    El GTFS de Renfe viene rellenado a ancho fijo, y la cabecera TAMBIEN: la
    ultima columna llega como ``"stop_sequence                    "``. Limpiar
    solo los valores deja las claves con relleno y ``row["stop_sequence"]``
    devuelve None, asi que hay que limpiar ambos. Un fallo asi no da un error de
    clave: da un valor vacio que revienta mucho mas abajo, o peor, un join que
    devuelve cero filas en silencio.
    """
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            yield {(k or "").strip(): (v or "").strip() for k, v in row.items()}


def _row_value(row: dict[str, str], name: str) -> str:
    """Lee siempre campos GTFS con strip: Renfe rellena columnas con espacios."""
    return (row.get(name) or "").strip()


def _load_routes(path: Path) -> dict[str, str]:
    routes: dict[str, str] = {}
    for row in _gtfs_rows(path):
        route_id = _row_value(row, "route_id")
        routes[route_id] = _row_value(row, "route_short_name")
    return routes


def _load_stop_names(path: Path) -> dict[str, str]:
    stops: dict[str, str] = {}
    for row in _gtfs_rows(path):
        stops[_row_value(row, "stop_id")] = _row_value(row, "stop_name")
    return stops


def _load_service_ids(path: Path) -> set[str]:
    services: set[str] = set()
    for row in _gtfs_rows(path):
        services.add(_row_value(row, "service_id"))
    return services


def _load_madrid_trips(
    path: Path, routes: dict[str, str], service_ids: set[str]
) -> dict[str, Trip]:
    trips: dict[str, Trip] = {}
    for row in _gtfs_rows(path):
        trip_id = _row_value(row, "trip_id")
        service_id = _row_value(row, "service_id")
        if not trip_id.startswith(MADRID_PREFIX) or service_id not in service_ids:
            continue
        route = routes.get(_row_value(row, "route_id"), "")
        trips[trip_id] = Trip(trip_id, service_id, route, block_id=_row_value(row, "block_id"))
    return trips


def _load_stop_times(path: Path, trips: dict[str, Trip]) -> None:
    """Añade sólo paradas Madrid, leyendo stop_times una fila cada vez."""
    for row in _gtfs_rows(path):
        trip = trips.get(_row_value(row, "trip_id"))
        if trip is None:
            continue
        sequence_text = _row_value(row, "stop_sequence")
        try:
            sequence = int(sequence_text)
        except ValueError as exc:
            raise ValueError(f"stop_sequence inválido para {trip.trip_id}: {sequence_text!r}") from exc
        trip.stops.append(
            StopTime(
                _row_value(row, "stop_id"),
                _row_value(row, "arrival_time"),
                _row_value(row, "departure_time"),
                sequence,
            )
        )


def _minutes(time_text: str) -> int:
    hour, minute, _second = time_text.split(":")
    return int(hour) * 60 + int(minute)


def _line_family(route: str) -> str:
    normalized = route.upper()
    if (
        len(normalized) >= LINE_VARIANT_MIN_LENGTH
        and normalized[-1] in {"A", "B"}
        and normalized[-2].isdigit()
    ):
        return normalized[:-1]
    return normalized


def _compatible(arrival: Trip, departure: Trip) -> bool:
    """Misma familia de linea, o un interlineado validado en esa cabecera."""
    fam_in, fam_out = _line_family(arrival.route), _line_family(departure.route)
    if fam_in == fam_out:
        return True
    permitidas = INTERLINE_TERMINI.get(departure.origin.stop_id)
    return permitidas is not None and {fam_in, fam_out} <= permitidas


class MinTurnaroundError(ValueError):
    """``MIN_TURNAROUND_BY_TERMINUS`` mal formada."""


def _log_error(message: str, **extra: object) -> None:
    """ERROR estructurado (Cloud Logging lee ``severity`` del JSON en stderr)."""
    print(
        json.dumps(
            {
                "severity": "ERROR",
                "message": message,
                "component": "regenerate-schedules",
                **extra,
            },
            ensure_ascii=False,
        ),
        file=sys.stderr,
    )


def parse_min_turnaround(text: str) -> dict[str, int]:
    """``"35607:5;70200:7"`` -> ``{"35607": 5, "70200": 7}``. Vacio -> ``{}``.

    Estricto: cualquier entrada mal formada lanza ``MinTurnaroundError``. Quien
    lee el entorno (``min_turnaround_from_env``) la convierte en ``{}``.
    """
    tabla: dict[str, int] = {}
    if not text.strip():
        return tabla
    for parte in text.split(MIN_TURNAROUND_ENTRY_SEPARATOR):
        entrada = parte.strip()
        if not entrada:
            continue  # tolera un ";" final
        trozos = entrada.split(MIN_TURNAROUND_VALUE_SEPARATOR)
        if len(trozos) != 2:  # noqa: PLR2004
            raise MinTurnaroundError(f"entrada sin forma parada:minutos: {parte!r}")
        parada, minutos = trozos[0].strip(), trozos[1].strip()
        if not parada.isascii() or not parada.isalnum():
            raise MinTurnaroundError(f"parada invalida: {parte!r}")
        # isdigit admite digitos no ASCII (U+0665); int() los aceptaria.
        if not minutos.isascii() or not minutos.isdigit():
            raise MinTurnaroundError(f"minutos no enteros o negativos: {parte!r}")
        valor = int(minutos)
        if valor > MAX_TURNAROUND_MINUTES:
            raise MinTurnaroundError(
                f"minutos > MAX_TURNAROUND_MINUTES ({MAX_TURNAROUND_MINUTES}): {parte!r}"
            )
        if parada in tabla:
            raise MinTurnaroundError(f"parada repetida: {parte!r}")
        tabla[parada] = valor
    return tabla


def min_turnaround_from_env() -> dict[str, int]:
    """Tabla del entorno, o la por defecto. NUNCA lanza: el job tiene que publicar.

    Una variable mal formada se registra como ERROR y se ignora ENTERA (``{}``,
    comportamiento de main): aplicar media tabla seria peor que ninguna.
    """
    texto = os.environ.get(MIN_TURNAROUND_ENV)
    if texto is None:
        return dict(DEFAULT_MIN_TURNAROUND_BY_TERMINUS)
    try:
        return parse_min_turnaround(texto)
    except MinTurnaroundError as exc:
        _log_error(
            f"{MIN_TURNAROUND_ENV} invalida, se ignora entera (sin maniobra minima): {exc}",
            value=texto,
        )
        return {}


def validate_min_turnaround(
    min_turnaround: dict[str, int], known_termini: set[str]
) -> dict[str, int]:
    """Quita (con ERROR) las paradas que no son origen de ningun viaje del GTFS."""
    desconocidas = sorted(set(min_turnaround) - known_termini)
    if desconocidas:
        _log_error(
            f"{MIN_TURNAROUND_ENV}: paradas que no son cabecera en el GTFS, se ignoran",
            unknown_stops=desconocidas,
        )
    return {k: v for k, v in min_turnaround.items() if k in known_termini}


def _gap(arrival: Trip, departure: Trip) -> int:
    return _minutes(departure.origin.departure_time) - _minutes(arrival.terminal.arrival_time)


def _block_links(trips: list[Trip]) -> dict[str, Turnaround]:
    """Enlaces declarados por ``block_id`` (solo bloques de dos o mas viajes)."""
    bloques: dict[str, list[Trip]] = defaultdict(list)
    for trip in trips:
        if trip.block_id and trip.stops:
            bloques[trip.block_id].append(trip)
    links: dict[str, Turnaround] = {}
    for viajes in bloques.values():
        if len(viajes) < 2:  # noqa: PLR2004 - un bloque de un viaje no enlaza nada
            continue
        viajes.sort(key=lambda t: _minutes(t.origin.departure_time))
        for previo, siguiente in pairwise(viajes):
            if previo.terminal.stop_id == siguiente.origin.stop_id and _gap(previo, siguiente) >= 0:
                links[siguiente.trip_id] = Turnaround(previo.trip_id, _gap(previo, siguiente))
    return links


def _link_turnarounds(
    trips: list[Trip], min_turnaround: dict[str, int] | None = None
) -> dict[str, Turnaround]:
    """Une llegadas terminales a salidas posteriores sin reutilizar material.

    Primero manda ``block_id`` cuando agrupa viajes de verdad. Para el resto se
    infiere: misma estacion (fin del entrante = origen del saliente), familia
    compatible (:func:`_compatible`) y salida entre 0 y
    ``MAX_TURNAROUND_MINUTES`` despues de la llegada.

    Procesar por hora de salida y consumir la llegada elegida hace el
    emparejamiento 1:1 y determinista: la primera salida se queda la llegada y
    ninguna otra puede reclamarla. Es lo que impide el fallo original, en el que
    142 llegadas se repartían entre 302 salidas.

    Entre varias llegadas compatibles y sin consumir se toma la MÁS PRÓXIMA. Que
    haya dos candidatas no hace ambiguo el enlace: en una cabecera concurrida lo
    normal es que haya varias llegadas en cuarenta minutos, y la otra la
    consumirá la salida siguiente. Exigir candidata única descartaba la mitad de
    los enlaces reales (47 % de cobertura frente al 90 % alcanzable) y dejaba sin
    aviso a andenes donde el enlace sí era determinable.

    Lo que SI es ambiguo es un empate (``AMBIGUITY_TIE_MINUTES``): dos llegadas
    a la par para una salida, o dos salidas a la par que podrian llevarse la
    misma llegada. El enlace se conserva, marcado ``ambiguous``.

    ``min_turnaround`` (por estacion de cabecera) exige que la salida sea al
    menos esos minutos POSTERIOR a la llegada: una llegada demasiado justa no
    forma esa salida y queda libre para la siguiente. Sin entrada, 0.
    """
    minimos = min_turnaround or {}

    def _cabe(arrival: Trip, departure: Trip) -> bool:
        minimo = minimos.get(departure.origin.stop_id, 0)
        return minimo <= _gap(arrival, departure) <= MAX_TURNAROUND_MINUTES

    links = _block_links(trips)
    used_arrivals: set[str] = {link.inbound_trip_id for link in links.values()}

    arrivals_by_station: dict[str, list[Trip]] = defaultdict(list)
    departures: list[Trip] = []
    for trip in trips:
        if not trip.stops:
            continue
        arrivals_by_station[trip.terminal.stop_id].append(trip)
        departures.append(trip)

    def candidatos(departure: Trip) -> list[Trip]:
        return [
            arrival
            for arrival in arrivals_by_station[departure.origin.stop_id]
            if arrival.trip_id not in used_arrivals
            and arrival.trip_id != departure.trip_id
            and _compatible(arrival, departure)
            and _cabe(arrival, departure)
        ]

    ordered_departures = sorted(
        departures, key=lambda trip: (_minutes(trip.origin.departure_time), trip.trip_id)
    )
    for departure in ordered_departures:
        if departure.trip_id in links:
            continue
        candidates = sorted(candidatos(departure), key=lambda a: _gap(a, departure))
        if not candidates:
            continue
        inbound = candidates[0]
        gap = _gap(inbound, departure)
        empate_llegadas = (
            len(candidates) > 1 and _gap(candidates[1], departure) - gap <= AMBIGUITY_TIE_MINUTES
        )
        empate_salidas = any(
            otra.trip_id != departure.trip_id
            and otra.trip_id not in links
            and otra.origin.stop_id == departure.origin.stop_id
            and _compatible(inbound, otra)
            and _cabe(inbound, otra)
            and abs(_gap(inbound, otra) - gap) <= AMBIGUITY_TIE_MINUTES
            for otra in ordered_departures
        )
        used_arrivals.add(inbound.trip_id)
        links[departure.trip_id] = Turnaround(
            inbound.trip_id, gap, ambiguous=empate_llegadas or empate_salidas
        )
    return links


def build_schedules(
    gtfs_dir: Path, min_turnaround: dict[str, int] | None = None
) -> dict[str, dict[str, object]]:
    """Construye todos los días de servicio Madrid desde un directorio GTFS."""
    routes = _load_routes(gtfs_dir / "routes.txt")
    stop_names = _load_stop_names(gtfs_dir / "stops.txt")
    service_ids = _load_service_ids(gtfs_dir / "calendar.txt")
    trips = _load_madrid_trips(gtfs_dir / "trips.txt", routes, service_ids)
    _load_stop_times(gtfs_dir / "stop_times.txt", trips)

    by_service: dict[str, list[Trip]] = defaultdict(list)
    for trip in trips.values():
        if trip.stops:
            by_service[trip.service_id].append(trip)

    if min_turnaround:
        cabeceras = {trip.origin.stop_id for trips_ in by_service.values() for trip in trips_}
        min_turnaround = validate_min_turnaround(min_turnaround, cabeceras)

    schedules: dict[str, dict[str, object]] = {}
    for service_id, service_trips in by_service.items():
        links = _link_turnarounds(service_trips, min_turnaround)
        stations: dict[str, list[dict[str, object]]] = defaultdict(list)
        for trip in service_trips:
            terminal = trip.terminal
            destination = stop_names.get(terminal.stop_id, terminal.stop_id)
            link = links.get(trip.trip_id)
            for stop in sorted(trip.stops, key=lambda item: item.sequence):
                stations[stop.stop_id].append(
                    {
                        "trip": trip.trip_id,
                        "t": stop.departure_time,
                        "seq": stop.sequence,
                        "dest": destination,
                        "route": trip.route,
                        "terminates": stop.sequence == terminal.sequence,
                        "from": link.inbound_trip_id if link else None,
                        "slack": link.slack_minutes if link else None,
                        # Solo cuando es verdad: no engordar el JSON con falses.
                        **({"amb": True} if link and link.ambiguous else {}),
                    }
                )
        for departures in stations.values():
            departures.sort(key=lambda stop: (str(stop["t"]), str(stop["trip"])))
        schedules[service_id] = {"service": service_id, "stations": dict(stations)}
    return schedules


def main() -> int:
    """Descarga, genera completamente y sólo entonces publica los horarios."""
    try:
        outcome = download_gtfs_static(GTFS_STATIC_DIR)
    except GtfsStaticUnavailableError as exc:
        print(f"ERROR FATAL: no se publican horarios: {exc}", file=sys.stderr)
        return 1

    schedules = build_schedules(outcome.version_dir, min_turnaround_from_env())
    publisher = publisher_from_env()
    for service_id, schedule in sorted(schedules.items()):
        publisher.publish(
            f"{SCHEDULES_PREFIX}/{service_id}.json",
            json.dumps(schedule, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
            cache_control=SCHEDULE_CACHE_CONTROL,
        )
    print(f"horarios publicados: {len(schedules)} días de servicio")
    if len(schedules) < MIN_SERVICE_DAYS:
        # Se publica igualmente lo que hay (mejor algo que nada), pero el job
        # termina en error para que Cloud Run lo marque y salte la alerta.
        print(
            json.dumps(
                {
                    "severity": "ERROR",
                    "message": f"solo {len(schedules)} días de servicio Madrid en el GTFS "
                    f"(mínimo {MIN_SERVICE_DAYS})",
                    "component": "regenerate-schedules",
                    "services": sorted(schedules),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return EXIT_TOO_FEW_DAYS
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
