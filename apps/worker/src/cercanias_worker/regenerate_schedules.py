"""Regenera los horarios diarios de Cercanías Madrid desde el GTFS estático."""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
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
LINE_VARIANT_MIN_LENGTH = 3
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
        trips[trip_id] = Trip(trip_id, service_id, route)
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


def _link_turnarounds(trips: list[Trip]) -> dict[str, Turnaround]:
    """Une llegadas terminales a salidas posteriores sin reutilizar material.

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
    """
    arrivals_by_station: dict[str, list[Trip]] = defaultdict(list)
    departures: list[Trip] = []
    for trip in trips:
        if not trip.stops:
            continue
        arrivals_by_station[trip.terminal.stop_id].append(trip)
        departures.append(trip)

    links: dict[str, Turnaround] = {}
    used_arrivals: set[str] = set()
    ordered_departures = sorted(
        departures, key=lambda trip: (_minutes(trip.origin.departure_time), trip.trip_id)
    )
    for departure in ordered_departures:
        departure_time = _minutes(departure.origin.departure_time)
        candidates = [
            arrival
            for arrival in arrivals_by_station[departure.origin.stop_id]
            if arrival.trip_id not in used_arrivals
            and arrival.trip_id != departure.trip_id
            and _line_family(arrival.route) == _line_family(departure.route)
            and 0 <= departure_time - _minutes(arrival.terminal.arrival_time) <= MAX_TURNAROUND_MINUTES
        ]
        if not candidates:
            continue
        inbound = min(
            candidates, key=lambda a: departure_time - _minutes(a.terminal.arrival_time)
        )
        used_arrivals.add(inbound.trip_id)
        links[departure.trip_id] = Turnaround(
            inbound.trip_id, departure_time - _minutes(inbound.terminal.arrival_time)
        )
    return links


def build_schedules(gtfs_dir: Path) -> dict[str, dict[str, object]]:
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

    schedules: dict[str, dict[str, object]] = {}
    for service_id, service_trips in by_service.items():
        links = _link_turnarounds(service_trips)
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

    schedules = build_schedules(outcome.version_dir)
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
