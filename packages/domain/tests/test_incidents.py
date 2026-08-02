from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cercanias_domain.incidents import (
    AlertCooldown,
    IncidentSeverity,
    IncidentThresholds,
    TrainDelay,
    detect_incidents,
    format_incident_message,
)

T0 = datetime(2026, 8, 1, 8, 0, tzinfo=UTC)


def _trenes(route: str, *delays: int) -> list[TrainDelay]:
    return [TrainDelay(f"1011S{i}{route}", route, d) for i, d in enumerate(delays)]


def test_un_solo_tren_retrasado_no_es_incidencia() -> None:
    """Ruido operativo normal. Avisar por esto entrena a ignorar los avisos."""
    assert detect_incidents(_trenes("C4", 1800, 0, 0, 0)) == ()


def test_varios_trenes_de_la_misma_linea_si_son_incidencia() -> None:
    incidencias = detect_incidents(_trenes("C4", 600, 700, 800, 0))
    assert len(incidencias) == 1
    inc = incidencias[0]
    assert inc.route_group == "C4"
    assert inc.affected_trains == 3
    assert inc.total_trains == 4
    assert inc.max_delay_seconds == 800


def test_retrasos_repartidos_entre_lineas_no_disparan_nada() -> None:
    """Tres lineas con un tren tocado cada una no es una incidencia de linea."""
    delays = _trenes("C1", 900) + _trenes("C2", 900) + _trenes("C3", 900)
    assert detect_incidents(delays) == ()


def test_severidad_escala_con_el_retraso_maximo() -> None:
    leve = detect_incidents(_trenes("C4", 400, 400, 400))[0]
    grave = detect_incidents(_trenes("C4", 400, 400, 1200))[0]
    assert leve.severity is IncidentSeverity.MINOR
    assert grave.severity is IncidentSeverity.MAJOR


def test_incidencias_ordenadas_por_gravedad() -> None:
    """Si hay que recortar el mensaje, que sobreviva lo peor."""
    delays = _trenes("C1", 400, 400, 400) + _trenes("C5", 2000, 2000, 2000)
    incidencias = detect_incidents(delays)
    assert [i.route_group for i in incidencias] == ["C5", "C1"]


def test_umbrales_configurables() -> None:
    delays = _trenes("C4", 200, 200)
    assert detect_incidents(delays) == ()
    laxos = IncidentThresholds(delay_seconds=100, min_trains=2)
    assert len(detect_incidents(delays, laxos)) == 1


# --- enfriamiento -------------------------------------------------------------


def _incidencia(max_delay: int):  # type: ignore[no-untyped-def]
    return detect_incidents(_trenes("C4", max_delay, max_delay, max_delay))[0]


def test_primera_incidencia_siempre_avisa() -> None:
    assert AlertCooldown().should_alert(_incidencia(600), T0) is True


def test_repeticion_inmediata_se_silencia() -> None:
    """Sin esto, cada pasada del bucle reenviaria la misma incidencia."""
    cooldown = AlertCooldown()
    inc = _incidencia(600)
    cooldown.record(inc, T0)
    assert cooldown.should_alert(inc, T0 + timedelta(minutes=1)) is False


def test_tras_el_enfriamiento_vuelve_a_avisar() -> None:
    cooldown = AlertCooldown()
    inc = _incidencia(600)
    cooldown.record(inc, T0)
    assert cooldown.should_alert(inc, T0 + timedelta(minutes=31)) is True


def test_escalada_de_severidad_rompe_el_enfriamiento() -> None:
    """Quien recibio 'retrasos de 10 min' debe enterarse de que ya son graves."""
    cooldown = AlertCooldown()
    cooldown.record(_incidencia(600), T0)
    grave = _incidencia(1200)
    assert grave.severity is IncidentSeverity.MAJOR
    assert cooldown.should_alert(grave, T0 + timedelta(minutes=2)) is True


def test_empeoramiento_material_rompe_el_enfriamiento() -> None:
    cooldown = AlertCooldown()
    cooldown.record(_incidencia(1000), T0)
    assert cooldown.should_alert(_incidencia(1700), T0 + timedelta(minutes=2)) is True


def test_empeoramiento_trivial_no_rompe_el_enfriamiento() -> None:
    """De 16 a 17 minutos no le sirve a nadie; reenviarlo es spam."""
    cooldown = AlertCooldown()
    cooldown.record(_incidencia(1000), T0)
    assert cooldown.should_alert(_incidencia(1060), T0 + timedelta(minutes=2)) is False


def test_el_enfriamiento_es_por_linea() -> None:
    cooldown = AlertCooldown()
    c4 = detect_incidents(_trenes("C4", 600, 600, 600))[0]
    c5 = detect_incidents(_trenes("C5", 600, 600, 600))[0]
    cooldown.record(c4, T0)
    assert cooldown.should_alert(c5, T0 + timedelta(minutes=1)) is True


# --- formato ------------------------------------------------------------------


def test_mensaje_incluye_atribucion_obligatoria() -> None:
    """Un aviso de retrasos sin atribucion se lee como comunicado oficial."""
    mensaje = format_incident_message(detect_incidents(_trenes("C4", 600, 600, 600)))
    assert "Renfe (CC BY 4.0)" in mensaje
    assert "sin relación con Renfe ni Adif" in mensaje


def test_mensaje_traduce_segundos_a_algo_legible() -> None:
    mensaje = format_incident_message(detect_incidents(_trenes("C4", 5400, 5400, 5400)))
    assert "1 h 30 min" in mensaje, mensaje
    assert "5400" not in mensaje, "no se muestran segundos crudos al usuario"
