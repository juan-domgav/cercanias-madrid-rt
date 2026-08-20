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


# --- aviso del enlace de cabecera ---------------------------------------------


def _con_enlace(num: str, hora: str, entrante: str, dest: str = "Parla") -> ScheduledStop:
    return ScheduledStop(f"{SERV}{num}C4b", hora, "C4b", dest, False, f"{SERV}{entrante}C4b")


def test_avisa_si_el_enlace_entrante_viene_tarde() -> None:
    """El tren que hara este servicio todavia no ha llegado a cabecera."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 1500, "1")}  # +25 min
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 25
    assert d.at_risk is True


def test_el_aviso_NO_altera_la_hora_prevista() -> None:
    """Es un aviso, no una prediccion: parte del retraso lo absorbe la maniobra
    y a veces el servicio se suprime."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 1500, "1")}
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.expected_hhmm == "09:05", "la hora prevista sigue siendo la programada"
    assert d.delay_minutes is None, "no se inventa un retraso propio"


def test_el_dato_propio_manda_sobre_el_del_enlace() -> None:
    """En cuanto el tren aparece con su retraso, el aviso del enlace sobra."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    vivos = {
        f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 1500, "1"),
        f"{SERV}20432C4b": LiveTrain(f"{SERV}20432C4b", 120, "3"),
    }
    d = next_departures(paradas, vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.delay_minutes == 2
    assert d.inbound_delay_minutes is None
    assert d.at_risk is False


def test_retraso_absorbido_por_la_maniobra_de_ese_enlace_no_avisa() -> None:
    """Sustituye a un test del umbral global de 8 min.

    Aquel fijaba una mediana de poblacion como umbral para CADA enlace, que es
    justo el error que encontro la revision adversarial: con 12 min de maniobra
    un retraso de 6 se absorbe entero y avisar seria una falsa alarma.
    """
    parada = ScheduledStop(f"{SERV}20432C4b", "09:05:00", "C4b", "Parla", False,
                           f"{SERV}20431C4b", 12)
    vivos = {f"{SERV}20431C4b": LiveTrain(f"{SERV}20431C4b", 360, "1")}  # +6 min
    d = next_departures([parada], vivos, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None
    assert d.at_risk is False


def test_sin_enlace_conocido_no_se_avisa() -> None:
    paradas = [_parada("20432", "09:05:00")]
    d = next_departures(paradas, {}, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None


def test_enlace_no_localizado_no_avisa() -> None:
    """Si el entrante tampoco se ve, no hay nada que decir: el silencio es
    preferible a insinuar un problema que no consta."""
    paradas = [_con_enlace("20432", "09:05:00", "20431")]
    d = next_departures(paradas, {}, DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None


# --- umbral relativo a la maniobra de CADA enlace ------------------------------
# Hallazgo de la revision adversarial: un umbral global invertia la decision en
# los dos extremos del rango real de maniobras (0 a 40 min).


def _enlace(num: str, hora: str, entrante: str, slack: int | None) -> ScheduledStop:
    return ScheduledStop(f"{SERV}{num}C4b", hora, "C4b", "Parla", False,
                         f"{SERV}{entrante}C4b", slack)


def _vivo(entrante: str, minutos: int) -> dict[str, LiveTrain]:
    tid = f"{SERV}{entrante}C4b"
    return {tid: LiveTrain(tid, minutos * 60, "1")}


def test_maniobra_larga_absorbe_el_retraso_y_no_avisa() -> None:
    """35 min de margen y +10 de retraso: el tren llega con 25 de sobra.
    El umbral global de 8 min daba aqui una falsa alarma."""
    d = next_departures([_enlace("20432", "09:35:00", "20431", 35)], _vivo("20431", 10),
                        DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None
    assert d.at_risk is False


def test_maniobra_nula_avisa_con_retraso_pequeno() -> None:
    """Sin margen, +7 significa que el tren NO puede estar en el anden.
    El umbral global de 8 min guardaba silencio en este caso."""
    d = next_departures([_enlace("20432", "09:00:00", "20431", 0)], _vivo("20431", 7),
                        DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 7
    assert d.at_risk is True


def test_retraso_trivial_nunca_avisa_aunque_no_haya_maniobra() -> None:
    d = next_departures([_enlace("20432", "09:00:00", "20431", 0)], _vivo("20431", 2),
                        DepartureQuery(now_minutes=8*60+55, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes is None


def test_sin_maniobra_conocida_se_usa_el_suelo() -> None:
    d = next_departures([_enlace("20432", "09:05:00", "20431", None)], _vivo("20431", 10),
                        DepartureQuery(now_minutes=9*60, service_prefix=SERV))[0]
    assert d.inbound_delay_minutes == 10
