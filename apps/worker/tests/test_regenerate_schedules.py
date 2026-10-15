from __future__ import annotations

from pathlib import Path

import pytest

import cercanias_worker.regenerate_schedules as module
from cercanias_connectors_renfe.gtfs_static_downloader import GtfsStaticUnavailableError


def _trip(
    trip_id: str, route: str, origin: tuple[str, str], terminal: tuple[str, str]
) -> module.Trip:
    return module.Trip(
        trip_id,
        trip_id[:5],
        route,
        [
            module.StopTime(origin[0], origin[1], origin[1], 1),
            module.StopTime(terminal[0], terminal[1], terminal[1], 2),
        ],
    )


def test_turnaround_arrival_is_consumed_by_only_one_departure() -> None:
    arrival = _trip("1031VC4-arrival", "C4a", ("B", "07:30:00"), ("A", "08:00:00"))
    first = _trip("1031VC4-first", "C4b", ("A", "08:10:00"), ("B", "08:30:00"))
    second = _trip("1031VC4-second", "C4", ("A", "08:15:00"), ("B", "08:35:00"))

    links = module._link_turnarounds([arrival, first, second])

    assert links[first.trip_id] == module.Turnaround(arrival.trip_id, 10)
    assert second.trip_id not in links


def test_turnaround_rejects_a_different_line_family() -> None:
    arrival = _trip("1031VC4-arrival", "C4a", ("B", "07:30:00"), ("A", "08:00:00"))
    departure = _trip("1031VC7-departure", "C7", ("A", "08:10:00"), ("B", "08:30:00"))

    assert module._link_turnarounds([arrival, departure]) == {}


def test_turnaround_takes_the_closest_of_several_arrivals() -> None:
    """Que haya dos llegadas compatibles no hace ambiguo el enlace.

    En una cabecera concurrida lo normal es que haya varias llegadas en cuarenta
    minutos; la otra la consumira la salida siguiente. Exigir candidata unica
    descartaba la mitad de los enlaces reales (47 % frente al 90 % alcanzable).
    """
    early = _trip("1031VC1-early", "C1", ("B", "07:30:00"), ("A", "08:00:00"))
    late = _trip("1031VC1-late", "C1", ("B", "07:35:00"), ("A", "08:05:00"))
    departure = _trip("1031VC1-departure", "C1", ("A", "08:10:00"), ("B", "08:30:00"))

    enlaces = module._link_turnarounds([early, late, departure])
    # La de las 08:05 esta mas cerca de la salida de las 08:10 que la de las 08:00.
    assert enlaces["1031VC1-departure"] == module.Turnaround("1031VC1-late", 5)


def test_turnaround_leaves_the_other_arrival_free_for_the_next_departure() -> None:
    """Consumir 1:1 no debe dejar huerfana a la salida siguiente: la llegada que
    no se uso tiene que quedar disponible para ella."""
    early = _trip("1031VC1-early", "C1", ("B", "07:30:00"), ("A", "08:00:00"))
    late = _trip("1031VC1-late", "C1", ("B", "07:35:00"), ("A", "08:05:00"))
    dep1 = _trip("1031VC1-dep1", "C1", ("A", "08:10:00"), ("B", "08:30:00"))
    dep2 = _trip("1031VC1-dep2", "C1", ("A", "08:20:00"), ("B", "08:40:00"))
    enlaces = module._link_turnarounds([early, late, dep1, dep2])
    assert enlaces["1031VC1-dep1"].inbound_trip_id == "1031VC1-late"
    assert enlaces["1031VC1-dep2"].inbound_trip_id == "1031VC1-early"
    assert len({e.inbound_trip_id for e in enlaces.values()}) == 2, "sin reutilizar"


def test_schedule_uses_sequence_one_as_trip_origin(tmp_path: Path) -> None:
    (tmp_path / "routes.txt").write_text(
        "route_id,route_short_name\n10T0001C4  ,C4a  \n", encoding="utf-8"
    )
    (tmp_path / "stops.txt").write_text(
        "stop_id,stop_name\nA  ,Origen  \nB  ,Destino  \n", encoding="utf-8"
    )
    (tmp_path / "calendar.txt").write_text("service_id\n1031V  \n", encoding="utf-8")
    (tmp_path / "trips.txt").write_text(
        "route_id,service_id,trip_id\n10T0001C4,1031V,1031V001C4a\n", encoding="utf-8"
    )
    (tmp_path / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "1031V001C4a,08:20:00,08:20:00,B,2\n"
        "1031V001C4a,08:00:00,08:05:00,A,1\n",
        encoding="utf-8",
    )

    schedule = module.build_schedules(tmp_path)["1031V"]
    origin_stop = schedule["stations"]["A"][0]

    assert origin_stop["seq"] == 1
    assert origin_stop["t"] == "08:05:00"
    assert origin_stop["dest"] == "Destino"
    assert schedule["stations"]["B"][0]["terminates"] is True


def test_main_does_not_create_a_publisher_when_gtfs_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*_args: object, **_kwargs: object) -> None:
        raise GtfsStaticUnavailableError([])

    monkeypatch.setattr(module, "download_gtfs_static", unavailable)
    monkeypatch.setattr(
        module,
        "publisher_from_env",
        lambda: pytest.fail("el bucket no debe tocarse tras un fallo de descarga"),
    )

    assert module.main() == 1


def test_main_fails_when_too_few_service_days(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class Outcome:
        version_dir = tmp_path

    published: list[str] = []

    class Publisher:
        def publish(self, key: str, *_args: object, **_kwargs: object) -> str:
            published.append(key)
            return key

    monkeypatch.setattr(module, "download_gtfs_static", lambda *_a, **_k: Outcome())
    monkeypatch.setattr(
        module, "build_schedules", lambda _d: {"1085X": {"service": "1085X", "stations": {}}}
    )
    monkeypatch.setattr(module, "publisher_from_env", Publisher)

    assert module.main() == module.EXIT_TOO_FEW_DAYS
    assert published == ["horarios/1085X.json"]
    assert '"severity": "ERROR"' in capsys.readouterr().err
