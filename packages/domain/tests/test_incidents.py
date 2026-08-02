from __future__ import annotations

from datetime import UTC, datetime, timedelta

from cercanias_domain.incidents import (
    AlertCooldown,
    AlertPolicy,
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


# --- politica de notificacion (distinta de la deteccion) ----------------------


def test_grave_siempre_se_notifica() -> None:
    inc = detect_incidents(_trenes("C7", 4080, 4080, 4080))[0]
    assert inc.severity is IncidentSeverity.MAJOR
    assert AlertPolicy().should_notify(inc) is True


def test_leve_y_minoritario_no_se_notifica() -> None:
    """El caso real de la C4 el 02/08/2026: 7 de 19 trenes con 5-8 min.

    Se DETECTA (alimenta el snapshot y el mapa) pero no se notifica: en una red
    de cercanias eso es una tarde normal, y colarlo en el mismo mensaje que un
    fallo de 68 minutos diluye la senal.
    """
    delays = _trenes("C4", *([480] * 7), *([0] * 12))
    inc = detect_incidents(delays)[0]
    assert inc.severity is IncidentSeverity.MINOR
    assert inc.affected_trains == 7
    assert inc.total_trains == 19
    assert AlertPolicy().should_notify(inc) is False


def test_leve_pero_generalizado_si_se_notifica() -> None:
    """Media linea con retraso leve es una disrupcion que el viajero nota."""
    delays = _trenes("C3", *([480] * 6), *([0] * 4))
    inc = detect_incidents(delays)[0]
    assert inc.severity is IncidentSeverity.MINOR
    assert inc.affected_ratio >= 0.5
    assert AlertPolicy().should_notify(inc) is True


def test_el_umbral_queda_por_encima_del_ruido_observado() -> None:
    """La C4 oscilo entre 32 % y 42 % en cinco pasadas seguidas.

    Con el liston en 0,4 habria entrado y salido del aviso entre pasadas. Este
    test fija que ninguna de las proporciones observadas ese dia notifica.
    """
    politica = AlertPolicy()
    for afectados in (6, 7, 8):  # de 19 trenes: 32 %, 37 %, 42 %
        delays = _trenes("C4", *([480] * afectados), *([0] * (19 - afectados)))
        inc = detect_incidents(delays)[0]
        assert politica.should_notify(inc) is False, f"{afectados}/19 no deberia notificar"


def test_umbral_de_proporcion_configurable() -> None:
    delays = _trenes("C4", *([480] * 7), *([0] * 12))
    inc = detect_incidents(delays)[0]
    assert AlertPolicy(notify_affected_ratio=0.3).should_notify(inc) is True
