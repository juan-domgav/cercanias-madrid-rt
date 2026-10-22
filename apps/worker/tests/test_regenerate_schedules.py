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


# --- RB-007: block_id, interlineado y ambiguedad -------------------------------


def test_block_id_compartido_manda_sobre_la_inferencia() -> None:
    """Si el GTFS agrupa viajes en un mismo block_id, ese es el enlace aunque haya
    otra llegada mas cercana a la salida."""
    llegada_bloque = _trip("1092XB-in", "C1", ("B", "07:30:00"), ("A", "08:00:00"))
    llegada_bloque.block_id = "BLK1"
    otra = _trip("1092XO-in", "C1", ("B", "07:35:00"), ("A", "08:06:00"))
    salida = _trip("1092XB-out", "C1", ("A", "08:10:00"), ("B", "08:40:00"))
    salida.block_id = "BLK1"
    enlaces = module._link_turnarounds([llegada_bloque, otra, salida])
    assert enlaces["1092XB-out"] == module.Turnaround("1092XB-in", 10)


def test_block_id_de_un_solo_viaje_no_cuenta() -> None:
    """El GTFS de Renfe trae block_id = numero de tren (uno por viaje): no dice
    nada del material y se ignora."""
    llegada = _trip("1092XC1-in", "C1", ("B", "07:30:00"), ("A", "08:00:00"))
    llegada.block_id = "1092XC1-in"
    salida = _trip("1092XC1-out", "C1", ("A", "08:10:00"), ("B", "08:40:00"))
    salida.block_id = "1092XC1-out"
    assert module._link_turnarounds([llegada, salida])["1092XC1-out"].inbound_trip_id == "1092XC1-in"


def test_interlineado_permitido_en_cabecera_validada() -> None:
    """Cercedilla (12006): el C8 que llega vuelve como C9."""
    llegada = _trip("1092X36330C8b", "C8b", ("10203", "08:00:00"), ("12006", "09:00:00"))
    salida = _trip("1092X36901C9", "C9", ("12006", "09:05:00"), ("12010", "09:40:00"))
    enlaces = module._link_turnarounds([llegada, salida])
    assert enlaces["1092X36901C9"] == module.Turnaround("1092X36330C8b", 5)


def test_interlineado_no_permitido_en_un_nudo() -> None:
    """Chamartin (17000) junta C1, C2, C3, C8 y C10: cruzar familias alli es
    adivinar. Solo se enlaza dentro de la misma familia."""
    llegada = _trip("1092X1C1", "C1", ("X", "08:00:00"), ("17000", "08:30:00"))
    salida = _trip("1092X2C3", "C3", ("17000", "08:35:00"), ("Y", "09:00:00"))
    assert module._link_turnarounds([llegada, salida]) == {}


def test_dos_salidas_candidatas_casi_a_la_vez_es_ambiguo() -> None:
    """Una llegada y dos salidas de la misma familia separadas 1 min: no se sabe
    cual hace el tren que llega. Se enlaza (para el aviso) pero marcado ambiguo,
    y el tablon NO estima desde el."""
    llegada = _trip("1092XC2-in", "C2", ("B", "07:30:00"), ("A", "08:00:00"))
    sal1 = _trip("1092XC2-o1", "C2", ("A", "08:10:00"), ("B", "08:40:00"))
    sal2 = _trip("1092XC2-o2", "C2", ("A", "08:11:00"), ("C", "08:45:00"))
    enlaces = module._link_turnarounds([llegada, sal1, sal2])
    assert enlaces["1092XC2-o1"].ambiguous is True


def test_dos_llegadas_empatadas_es_ambiguo() -> None:
    a1 = _trip("1092XC2-a1", "C2", ("B", "07:30:00"), ("A", "08:00:00"))
    a2 = _trip("1092XC2-a2", "C2", ("C", "07:31:00"), ("A", "08:01:00"))
    sal = _trip("1092XC2-out", "C2", ("A", "08:10:00"), ("B", "08:40:00"))
    assert module._link_turnarounds([a1, a2, sal])["1092XC2-out"].ambiguous is True


def test_enlace_claro_no_es_ambiguo() -> None:
    llegada = _trip("1092XC4-in", "C4b", ("B", "07:30:00"), ("A", "08:00:00"))
    salida = _trip("1092XC4-out", "C4b", ("A", "08:14:00"), ("B", "08:40:00"))
    assert module._link_turnarounds([llegada, salida])["1092XC4-out"].ambiguous is False


def test_horario_publica_ambiguedad(tmp_path: Path) -> None:
    gtfs = tmp_path
    (gtfs / "routes.txt").write_text("route_id,route_short_name\nR2,C2\n")
    (gtfs / "stops.txt").write_text("stop_id,stop_name\nA,Alfa\nB,Beta\nC,Gamma\n")
    (gtfs / "calendar.txt").write_text(
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "1092X,0,0,1,0,0,0,0,20261021,20261021\n")
    (gtfs / "trips.txt").write_text(
        "route_id,service_id,trip_id,trip_headsign,wheelchair_accessible,block_id,shape_id\n"
        "R2,1092X,1092X1C2,,1,1092X1,s\nR2,1092X,1092X2C2,,1,1092X2,s\n"
        "R2,1092X,1092X3C2,,1,1092X3,s\n")
    (gtfs / "stop_times.txt").write_text(
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "1092X1C2,07:30:00,07:30:00,B,1\n1092X1C2,08:00:00,08:00:00,A,2\n"
        "1092X2C2,08:10:00,08:10:00,A,1\n1092X2C2,08:40:00,08:40:00,B,2\n"
        "1092X3C2,08:11:00,08:11:00,A,1\n1092X3C2,08:45:00,08:45:00,C,2\n")
    dia = module.build_schedules(gtfs)["1092X"]["stations"]
    salida = next(e for e in dia["A"] if e["trip"] == "1092X2C2")  # type: ignore[index]
    assert salida["from"] == "1092X1C2"
    assert salida["amb"] is True
