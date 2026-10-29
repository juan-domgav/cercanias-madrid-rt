from __future__ import annotations

from datetime import time

import pytest

from cercanias_domain.health import (
    FeedHealthInput,
    Invariant,
    Severity,
    band_for,
    evaluate_feed_health,
    is_service_hours,
)


def _input(**overrides: object) -> FeedHealthInput:
    """Pasada sana por defecto; cada test rompe solo lo que quiere probar."""
    base: dict[str, object] = {
        "local_time": time(16, 35),
        "entities_in_payload": 272,
        "entities_after_nucleo_filter": 101,
        "trips_resolved": 101,
        "trip_updates_total": 101,
        "trip_updates_with_delay": 98,
        "feed_skew_seconds": 17.0,
    }
    base.update(overrides)
    return FeedHealthInput(**base)  # type: ignore[arg-type]


def test_medicion_real_del_31_07_2026_es_sana() -> None:
    """Los numeros realmente observados en Fase 0 deben pasar todos los invariantes."""
    report = evaluate_feed_health(_input())
    assert report.healthy, [i.detail for i in report.failures]
    assert report.worst is Severity.OK


def test_filtro_a_cero_en_horario_de_servicio_es_critico() -> None:
    """R-1: el fallo mas caro de todos, y el que hoy no produce ningun error.

    El feed trae 272 entidades y ninguna se reconoce como Madrid. Eso no es
    "no hay trenes": es que el filtro ha dejado de reconocerlos, exactamente lo
    que paso al cambiar 1080 por 1010 (ADR-010).
    """
    report = evaluate_feed_health(
        _input(local_time=time(8, 30), entities_after_nucleo_filter=0, trips_resolved=0)
    )
    assert report.worst is Severity.CRITICAL
    fallo = next(i for i in report.invariants if i.name == "nucleo_filter_yield")
    assert fallo.severity is Severity.CRITICAL
    assert "272" in fallo.detail, "el detalle debe contrastar payload lleno con filtro vacio"


def test_filtro_a_cero_de_madrugada_es_normal() -> None:
    """La contrapartida: sin esto la alarma suena cada noche y se acaba ignorando."""
    report = evaluate_feed_health(
        _input(
            local_time=time(3, 45),
            entities_in_payload=12,
            entities_after_nucleo_filter=0,
            trips_resolved=0,
            trip_updates_total=0,
            trip_updates_with_delay=0,
        )
    )
    yield_inv = next(i for i in report.invariants if i.name == "nucleo_filter_yield")
    assert yield_inv.severity is Severity.OK
    assert report.worst is not Severity.CRITICAL


def test_payload_vacio_se_distingue_de_filtro_vacio() -> None:
    """Origen caido y filtro roto son averias distintas con responsables distintos."""
    report = evaluate_feed_health(
        _input(
            local_time=time(8, 30),
            entities_in_payload=0,
            entities_after_nucleo_filter=0,
            trips_resolved=0,
        )
    )
    payload = next(i for i in report.invariants if i.name == "payload_not_empty")
    assert payload.severity is Severity.CRITICAL
    assert "origen" in payload.detail.lower()


def test_caida_de_resolucion_de_trip_id_es_critica() -> None:
    """GTFS estatico desactualizado o cambio de formato de tripId."""
    report = evaluate_feed_health(_input(trips_resolved=40))
    inv = next(i for i in report.invariants if i.name == "trip_id_resolution")
    assert inv.severity is Severity.CRITICAL
    assert "39.6%" in inv.detail or "40/101" in inv.detail


def test_perdida_de_cobertura_de_retrasos_es_critica() -> None:
    report = evaluate_feed_health(_input(trip_updates_with_delay=10))
    inv = next(i for i in report.invariants if i.name == "delay_coverage")
    assert inv.severity is Severity.CRITICAL


def test_desfase_grande_avisa_pero_no_es_critico() -> None:
    """Un feed atrasado sigue siendo util; uno vacio no. La severidad lo refleja."""
    report = evaluate_feed_health(_input(feed_skew_seconds=600.0))
    inv = next(i for i in report.invariants if i.name == "feed_freshness")
    assert inv.severity is Severity.WARNING
    assert report.worst is Severity.WARNING


def test_recuento_fuera_de_banda_avisa_sin_ser_critico() -> None:
    report = evaluate_feed_health(_input(local_time=time(8, 30), entities_after_nucleo_filter=3))
    inv = next(i for i in report.invariants if i.name == "nucleo_filter_yield")
    assert inv.severity is Severity.WARNING


@pytest.mark.parametrize(
    ("hora", "esperado"),
    [
        (time(8, 30), "punta de mañana"),
        (time(13, 0), "valle"),
        (time(18, 30), "punta de tarde"),
        (time(23, 0), "última hora"),
        (time(2, 0), "madrugada (sin servicio)"),
        (time(0, 30), "última hora"),
    ],
)
def test_bandas_horarias_incluido_el_cruce_de_medianoche(hora: time, esperado: str) -> None:
    assert band_for(hora).label == esperado


@pytest.mark.parametrize(
    ("hora", "hay_servicio"),
    [
        (time(6, 0), True),
        (time(23, 59), True),
        (time(1, 0), True),
        (time(2, 0), False),
        (time(4, 59), False),
        (time(5, 0), True),
    ],
)
def test_horario_de_servicio_cruza_medianoche(hora: time, hay_servicio: bool) -> None:
    assert is_service_hours(hora) is hay_servicio


# --- delay_coverage sin suprimidos (RB-015) -----------------------------------


def _cobertura(report_input: FeedHealthInput) -> Invariant:
    report = evaluate_feed_health(report_input)
    return next(i for i in report.invariants if i.name == "delay_coverage")


def test_suprimidos_no_cuentan_en_la_cobertura_de_retrasos() -> None:
    """29/10/2026 ~12:57: 52/65 (80 %) con retraso y los 13 sin retraso eran CANCELED.

    Un tren suprimido nunca publica retraso. Contarlo en el denominador daba un
    critico falso; sin ellos la cobertura es 52/52.
    """
    inv = _cobertura(
        _input(
            local_time=time(12, 57),
            entities_after_nucleo_filter=60,
            trips_resolved=60,
            trip_updates_total=65,
            trip_updates_with_delay=52,
            trip_updates_cancelled=13,
        )
    )
    assert inv.severity is Severity.OK, inv.detail
    assert "52/52" in inv.detail
    assert "13" in inv.detail, "el detalle debe decir cuantos suprimidos se excluyen"
    assert inv.metrics["n_cancelados"] == 13


def test_cobertura_baja_entre_no_suprimidos_sigue_siendo_critica() -> None:
    """Excluir suprimidos no debe tapar una perdida real de retrasos."""
    inv = _cobertura(
        _input(trip_updates_total=65, trip_updates_with_delay=30, trip_updates_cancelled=13)
    )
    assert inv.severity is Severity.CRITICAL
    assert "30/52" in inv.detail
    assert inv.metrics["n_cancelados"] == 13


def test_retraso_de_un_suprimido_no_suma_al_numerador() -> None:
    """Si un suprimido trae ``delay``, sale de ambos lados: 39/52, no 52/52."""
    inv = _cobertura(
        _input(
            trip_updates_total=65,
            trip_updates_with_delay=52,
            trip_updates_cancelled=13,
            trip_updates_cancelled_with_delay=13,
        )
    )
    assert inv.severity is Severity.CRITICAL
    assert "39/52" in inv.detail


def test_todos_suprimidos_no_divide_por_cero_ni_es_critico() -> None:
    """Huelga o corte total: no hay viajes en marcha cuya cobertura evaluar.

    Mismo criterio que denominador 0 en el resto de tasas: OK «sin datos». Que no
    haya trenes en circulacion ya lo vigila ``nucleo_filter_yield``.
    """
    inv = _cobertura(
        _input(trip_updates_total=13, trip_updates_with_delay=0, trip_updates_cancelled=13)
    )
    assert inv.severity is Severity.OK
    assert "13" in inv.detail
    assert inv.metrics["n_cancelados"] == 13


def test_sin_suprimidos_la_cobertura_no_cambia() -> None:
    """Compatibilidad: sin el campo nuevo el calculo es el de siempre."""
    inv = _cobertura(_input())
    assert inv.severity is Severity.OK
    assert "98/101" in inv.detail
    assert inv.metrics["n_cancelados"] == 0


def test_otros_invariantes_no_llevan_metricas() -> None:
    report = evaluate_feed_health(_input())
    otros = [i for i in report.invariants if i.name != "delay_coverage"]
    assert all(dict(i.metrics) == {} for i in otros)
