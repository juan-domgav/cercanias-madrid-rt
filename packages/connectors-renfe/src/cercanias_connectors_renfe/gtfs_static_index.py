"""Índice construido a partir del GTFS estático — implementa ``StaticScheduleIndex``.

ANOMALÍA verificada en Fase 0 (ver docs/adr y cobertura-estaciones.md): el
``trips.txt`` servido por la cascada de C-1 (en la práctica, por
``ssl.renfe.com``, ver ``gtfs_static_downloader.py``) NO tiene columna
``direction_id``, a diferencia de lo asumido por la especificación base y
por el Anexo A.4 ("trip_id del RT -> trips.txt -> route_id, direction_id,
trip_headsign"). En su lugar, cada sentido de una línea es un ``route_id``
distinto: p. ej. ``10T0001C1`` (Príncipe Pío→Aeropuerto-T4) y ``10T0002C1``
(Aeropuerto-T4→Príncipe Pío) comparten ``route_short_name`` "C1" pero son
filas separadas en ``routes.txt``.

SUPUESTO ASUMIDO: ``direction_id`` se deriva por convención, no se lee de
ningún campo: para cada ``route_short_name`` se ordenan lexicográficamente
los ``route_id`` distintos que comparten esa etiqueta y se numeran 0, 1, 2...
Esto es estable mientras Renfe no reordene los ``route_id``, pero es una
convención nuestra, no un dato oficial. Se marca explícitamente
(``TripStaticInfo.direction_derived = True``) para que nadie lo confunda con
un campo GTFS estándar. ``trip_headsign`` casi siempre viene en blanco en
los datos observados (solo aparece el valor "CIVIS" en un subconjunto de
trips): no es una fuente fiable de la etiqueta de sentido.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from cercanias_domain.models import TripStaticInfo


@dataclass(frozen=True)
class StaticStopInfo:
    stop_id: str
    stop_name: str
    latitude: float | None
    longitude: float | None


def _open_csv(path: Path) -> csv.DictReader[str]:
    text = path.read_text(encoding="utf-8-sig")
    return csv.DictReader(io.StringIO(text))


def _clean(value: str | None) -> str:
    return (value or "").strip()


def _derive_route_directions(routes_path: Path) -> tuple[dict[str, int], dict[str, str]]:
    """Agrupa route_id por route_short_name y numera 0/1/... por orden lexicografico.

    Devuelve (direction_id_por_route_id, route_short_name_por_route_id).
    """
    route_ids_by_short_name: dict[str, list[str]] = defaultdict(list)
    short_name_by_route_id: dict[str, str] = {}

    for row in _open_csv(routes_path):
        route_id = _clean(row.get("route_id"))
        short_name = _clean(row.get("route_short_name"))
        if not route_id:
            continue
        short_name_by_route_id[route_id] = short_name
        route_ids_by_short_name[short_name].append(route_id)

    direction_by_route_id: dict[str, int] = {}
    for route_ids in route_ids_by_short_name.values():
        for index, route_id in enumerate(sorted(set(route_ids))):
            direction_by_route_id[route_id] = index

    return direction_by_route_id, short_name_by_route_id


def _read_trips(
    trips_path: Path,
    direction_by_route_id: dict[str, int],
    short_name_by_route_id: dict[str, str],
) -> dict[str, TripStaticInfo]:
    trips: dict[str, TripStaticInfo] = {}
    for row in _open_csv(trips_path):
        trip_id = _clean(row.get("trip_id"))
        route_id = _clean(row.get("route_id"))
        if not trip_id or not route_id:
            continue
        headsign = _clean(row.get("trip_headsign")) or None
        trips[trip_id] = TripStaticInfo(
            route_id=route_id,
            route_short_name=short_name_by_route_id.get(route_id, ""),
            direction_id=direction_by_route_id.get(route_id),
            trip_headsign=headsign,
            direction_derived=True,
        )
    return trips


def _read_stops(stops_path: Path) -> dict[str, StaticStopInfo]:
    stops: dict[str, StaticStopInfo] = {}
    for row in _open_csv(stops_path):
        stop_id = _clean(row.get("stop_id"))
        if not stop_id:
            continue
        lat_raw = row.get("stop_lat")
        lon_raw = row.get("stop_lon")
        try:
            lat = float(lat_raw) if lat_raw else None
        except ValueError:
            lat = None
        try:
            lon = float(lon_raw) if lon_raw else None
        except ValueError:
            lon = None
        stops[stop_id] = StaticStopInfo(
            stop_id=stop_id,
            stop_name=_clean(row.get("stop_name")),
            latitude=lat,
            longitude=lon,
        )
    return stops


class CsvStaticScheduleIndex:
    """Implementa ``cercanias_domain.ports.StaticScheduleIndex`` a partir de CSVs."""

    def __init__(
        self, trips: dict[str, TripStaticInfo], stops: dict[str, StaticStopInfo]
    ) -> None:
        self._trips = trips
        self._stops = stops

    def lookup_trip(self, trip_id: str) -> TripStaticInfo | None:
        return self._trips.get(trip_id)

    def has_stop(self, stop_id: str) -> bool:
        return stop_id in self._stops

    def get_stop(self, stop_id: str) -> StaticStopInfo | None:
        return self._stops.get(stop_id)

    def all_stops(self) -> dict[str, StaticStopInfo]:
        return dict(self._stops)

    def trip_count(self) -> int:
        return len(self._trips)


def build_static_schedule_index(gtfs_dir: Path) -> CsvStaticScheduleIndex:
    """Construye el indice a partir de un directorio con el GTFS estatico ya extraido."""
    direction_by_route_id, short_name_by_route_id = _derive_route_directions(
        gtfs_dir / "routes.txt"
    )
    trips = _read_trips(gtfs_dir / "trips.txt", direction_by_route_id, short_name_by_route_id)
    stops = _read_stops(gtfs_dir / "stops.txt")
    return CsvStaticScheduleIndex(trips, stops)
