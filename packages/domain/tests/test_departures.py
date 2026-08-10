from __future__ import annotations

from cercanias_domain.departures import (
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    destinations_available,
    format_minutes,
    next_departures,
    parse_hhmm_to_minutes,
    train_number,
)

SERV = "1020L"


def _parada(num: str, hora: str, dest: str = "Parla", termina: bool = False) -> ScheduledStop:
    return ScheduledStop(f"{SERV}{num}C4b", hora, "C4b", dest, termina)


def test_numero_de_tren_desde_el_trip_id() -> None:
    assert train_number("1020L20432C4b") == "20432"
    assert train_number("1019D27446C4b") == "27446"


def test_horas_mayores_de_medianoche() -> None:
    """R-4: GTFS expresa los servicios de madrugada como 24:15, 25:03..."""
    assert parse_hhmm_to_minutes("25:14:00") == 1514
    assert format_minutes(1514) == "01:14", "debe presentarse como hora de reloj"


def test_tren_que_termina_aqui_no_es_una_salida() -> None:
    """En Colmenar la mitad de las paradas del horario son llegadas."""
    paradas = [_parada("20430", "09:00:00", termina=True),
               _parada("20432", "09:05:00")]
    salidas = next_departures(paradas, {}, DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))
    assert [d.train_number for d in salidas] == ["20432"]


def test_se_combina_horario_con_retraso_en_vivo() -> None:
    paradas = [_parada("20432", "09:05:00")]
    vivos = {f"{SERV}20432C4b": LiveTrain(f"{SERV}20432C4b", 180, "3")}
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.scheduled_hhmm == "09:05"
    assert d.expected_hhmm == "09:08", "programada + retraso publicado"
    assert d.delay_minutes == 3
    assert d.platform == "3"
    assert d.located is True


def test_sin_tren_localizado_no_se_inventa_anden() -> None:
    """El anden solo existe en el feed en vivo. Mandar a alguien al anden
    equivocado es peor que decirle que no se sabe."""
    d = next_departures([_parada("20432", "09:05:00")], {},
                        DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.platform is None
    assert d.delay_minutes is None
    assert d.located is False
    assert d.expected_hhmm == d.scheduled_hhmm, "sin dato en vivo, la prevista es la programada"


def test_ordena_por_hora_prevista_no_por_programada() -> None:
    """Un tren con 20 min de retraso sale despues que otro puntual posterior."""
    paradas = [_parada("20430", "09:00:00"), _parada("20432", "09:10:00")]
    vivos = {f"{SERV}20430C4b": LiveTrain(f"{SERV}20430C4b", 1200, "1")}
    salidas = next_departures(paradas, vivos, DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))
    assert [d.train_number for d in salidas] == ["20432", "20430"]


def test_filtra_por_destino() -> None:
    """En Tres Cantos conviven los dos sentidos."""
    paradas = [_parada("1", "09:05:00", dest="Colmenar Viejo"),
               _parada("2", "09:06:00", dest="Parla")]
    salidas = next_departures(paradas, {}, DepartureQuery(
        now_minutes=9*60, service_prefix=SERV, destination="Colmenar Viejo"))
    assert len(salidas) == 1
    assert salidas[0].destination == "Colmenar Viejo"


def test_ignora_otros_dias_de_servicio() -> None:
    """El indice de calendario avanza a diario (ADR-012)."""
    ajena = ScheduledStop("1021M20432C4b", "09:05:00", "C4b", "Parla", False)
    assert next_departures([ajena], {}, DepartureQuery(now_minutes=9*60, service_prefix=SERV)) == ()


def test_margen_para_el_que_llega_corriendo() -> None:
    paradas = [_parada("20430", "09:00:00")]
    assert len(next_departures(paradas, {}, DepartureQuery(now_minutes=9*60+2, service_prefix=SERV))) == 1
    assert len(next_departures(paradas, {}, DepartureQuery(now_minutes=9*60+30, service_prefix=SERV))) == 0


def test_destinos_disponibles_excluye_los_que_terminan() -> None:
    paradas = [_parada("1", "09:00:00", dest="Colmenar Viejo"),
               _parada("2", "09:01:00", dest="Parla"),
               _parada("3", "09:02:00", dest="Colmenar Viejo", termina=True)]
    assert destinations_available(paradas, service_prefix=SERV) == ("Colmenar Viejo", "Parla")
