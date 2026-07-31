from __future__ import annotations

from pathlib import Path

from cercanias_connectors_renfe.gtfs_static_index import build_static_schedule_index

ROUTES_TXT = (
    "route_id,route_short_name,route_long_name,route_type\n"
    "10T0001C1,C1,Principe Pio-Aeropuerto,2\n"
    "10T0002C1,C1,Aeropuerto-Principe Pio,2\n"
    "10T0013C4a,C4a,Chamartin-El Escorial,2\n"
)

TRIPS_TXT = (
    "route_id,service_id,trip_id,trip_headsign\n"
    "10T0001C1,1010V,1010V19799C1,\n"
    "10T0002C1,1010V,1010V19800C1,\n"
    "10T0013C4a,1010V,1010V20316C4a,\n"
)

STOPS_TXT = (
    "stop_id,stop_name,stop_lat,stop_lon\n"
    "18000,Madrid-Atocha Cercanias,40.4066191,-3.6894391\n"
    "17000,Madrid-Chamartin,40.4711789,-3.6829524\n"
)


def _write_gtfs(tmp_path: Path) -> Path:
    (tmp_path / "routes.txt").write_text(ROUTES_TXT, encoding="utf-8")
    (tmp_path / "trips.txt").write_text(TRIPS_TXT, encoding="utf-8")
    (tmp_path / "stops.txt").write_text(STOPS_TXT, encoding="utf-8")
    return tmp_path


def test_lookup_trip_resolves_route_and_headsign(tmp_path: Path) -> None:
    gtfs_dir = _write_gtfs(tmp_path)
    index = build_static_schedule_index(gtfs_dir)
    info = index.lookup_trip("1010V19799C1")
    assert info is not None
    assert info.route_id == "10T0001C1"
    assert info.route_short_name == "C1"


def test_direction_id_derived_from_route_id_pair(tmp_path: Path) -> None:
    """SUPUESTO ASUMIDO: direction_id no existe en trips.txt; se deriva
    ordenando los route_id que comparten route_short_name."""
    gtfs_dir = _write_gtfs(tmp_path)
    index = build_static_schedule_index(gtfs_dir)

    info_a = index.lookup_trip("1010V19799C1")  # route_id 10T0001C1
    info_b = index.lookup_trip("1010V19800C1")  # route_id 10T0002C1
    assert info_a is not None
    assert info_b is not None
    assert info_a.direction_id == 0  # 10T0001C1 < 10T0002C1 lexicograficamente
    assert info_b.direction_id == 1
    assert info_a.direction_derived is True


def test_single_direction_route_gets_direction_zero(tmp_path: Path) -> None:
    gtfs_dir = _write_gtfs(tmp_path)
    index = build_static_schedule_index(gtfs_dir)
    info = index.lookup_trip("1010V20316C4a")
    assert info is not None
    assert info.direction_id == 0


def test_unknown_trip_id_returns_none(tmp_path: Path) -> None:
    gtfs_dir = _write_gtfs(tmp_path)
    index = build_static_schedule_index(gtfs_dir)
    assert index.lookup_trip("no-existe") is None


def test_get_stop(tmp_path: Path) -> None:
    gtfs_dir = _write_gtfs(tmp_path)
    index = build_static_schedule_index(gtfs_dir)
    stop = index.get_stop("17000")
    assert stop is not None
    assert stop.stop_name == "Madrid-Chamartin"
    assert index.trip_count() == 3
