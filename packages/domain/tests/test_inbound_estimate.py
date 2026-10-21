"""Contrato: salida estimada desde el enlace entrante (RB-007).

Caso real del 21/10/2026, Tres Cantos (C4b): los trenes hacia Parla salen de
Colmenar Viejo dando la vuelta a un tren que llega desde Parla. Renfe no publica
el viaje de vuelta en el feed hasta ~30 min antes de su salida de cabecera, asi
que el tablon decia "sin datos en vivo" con el entrante localizado y con
retraso. Ahora se estima: max(0, retraso_entrante - (maniobra - minimo)).
"""

from __future__ import annotations

from cercanias_domain.departures import (
    MIN_PHYSICAL_TURNAROUND_MINUTES,
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    estimate_from_inbound,
    next_departures,
)

SERV = "1092X"
ENTRANTE = f"{SERV}20479C4b"   # Parla -> Colmenar, llega a Colmenar 15:43
SALIENTE = f"{SERV}20476C4b"   # Colmenar 15:57 -> Parla (maniobra 14 min)
AHORA = 15 * 60 + 26


def _tres_cantos(slack: int | None = 14, ambiguous: bool = False,
                 inbound: str | None = ENTRANTE) -> ScheduledStop:
    """El saliente en Tres Cantos: parada INTERMEDIA, no su origen."""
    return ScheduledStop(
        SALIENTE, "16:03:00", "C4b", "Parla", False, inbound, slack,
        False, "17004", inbound_ambiguous=ambiguous,
    )


def _vivo(trip: str, delay_min: int | None, **kw: object) -> LiveTrain:
    return LiveTrain(trip, None if delay_min is None else delay_min * 60, None, **kw)  # type: ignore[arg-type]


def _q(**kw: object) -> DepartureQuery:
    base: dict[str, object] = {"now_minutes": AHORA, "service_prefix": SERV,
                               "estimate_from_inbound": True}
    base.update(kw)
    return DepartureQuery(**base)  # type: ignore[arg-type]


def test_formula_resta_la_holgura_de_maniobra() -> None:
    # Holgura = 14 - 3 = 11. Entrante +20 -> salida +9.
    assert estimate_from_inbound(20, 14) == 20 - (14 - MIN_PHYSICAL_TURNAROUND_MINUTES)


def test_formula_nunca_negativa() -> None:
    """Un entrante adelantado o con poco retraso NO adelanta la salida."""
    assert estimate_from_inbound(6, 14) == 0
    assert estimate_from_inbound(-4, 14) == 0, "un adelanto no se propaga"


def test_maniobra_mas_corta_que_el_minimo_suma() -> None:
    """Con 2 min de maniobra prevista, el tren no puede dar la vuelta en hora."""
    assert estimate_from_inbound(0, 2) == MIN_PHYSICAL_TURNAROUND_MINUTES - 2


def test_vuelta_ausente_con_entrante_retrasado_se_estima() -> None:
    """20479 va +20 y 20476 aun no esta en el feed: +9 estimado en Tres Cantos."""
    d = next_departures([_tres_cantos()], {ENTRANTE: _vivo(ENTRANTE, 20)}, _q())[0]
    assert d.delay_minutes is None, "no es un dato en vivo"
    assert d.located is False
    assert d.estimated_delay_minutes == 9
    assert d.delay_source == "inbound_estimate"
    assert d.expected_hhmm == "16:12"
    assert d.minutes_until == 16 * 60 + 12 - AHORA
    assert d.shows_on_time is False, "un estimado nunca se pinta como 'en hora' real"
    assert d.unconfirmed is False


def test_caso_juan_entrante_mas_6_holgura_14_estima_en_hora() -> None:
    """+6 se absorbe en 14 min de maniobra: estimado 0, no 'sin datos'."""
    d = next_departures([_tres_cantos()], {ENTRANTE: _vivo(ENTRANTE, 6)}, _q())[0]
    assert d.estimated_delay_minutes == 0
    assert d.delay_source == "inbound_estimate"
    assert d.expected_hhmm == "16:03"


def test_el_dato_real_siempre_manda() -> None:
    vivos = {ENTRANTE: _vivo(ENTRANTE, 20), SALIENTE: _vivo(SALIENTE, 2)}
    d = next_departures([_tres_cantos()], vivos, _q())[0]
    assert d.delay_minutes == 2
    assert d.estimated_delay_minutes is None
    assert d.delay_source == "live"
    assert d.expected_hhmm == "16:05"


def test_feed_viejo_no_estima() -> None:
    """Guard de PR #7: con el snapshot degradado no se proyecta nada."""
    d = next_departures([_tres_cantos()], {ENTRANTE: _vivo(ENTRANTE, 20)},
                        _q(estimate_from_inbound=False))[0]
    assert d.estimated_delay_minutes is None
    assert d.delay_source is None
    assert d.expected_hhmm == "16:03"


def test_por_defecto_no_estima() -> None:
    q = DepartureQuery(now_minutes=AHORA, service_prefix=SERV)
    d = next_departures([_tres_cantos()], {ENTRANTE: _vivo(ENTRANTE, 20)}, q)[0]
    assert d.estimated_delay_minutes is None


def test_entrante_suprimido_no_estima() -> None:
    vivos = {ENTRANTE: _vivo(ENTRANTE, 20, cancelled=True)}
    d = next_departures([_tres_cantos()], vivos, _q())[0]
    assert d.estimated_delay_minutes is None


def test_entrante_sin_retraso_publicado_no_estima() -> None:
    d = next_departures([_tres_cantos()], {ENTRANTE: _vivo(ENTRANTE, None)}, _q())[0]
    assert d.estimated_delay_minutes is None


def test_entrante_no_localizado_no_estima() -> None:
    d = next_departures([_tres_cantos()], {}, _q())[0]
    assert d.estimated_delay_minutes is None
    assert d.delay_source is None


def test_enlace_ambiguo_no_estima() -> None:
    """Dos candidatos casi empatados: no se sabe que tren hace el servicio."""
    d = next_departures([_tres_cantos(ambiguous=True)], {ENTRANTE: _vivo(ENTRANTE, 20)}, _q())[0]
    assert d.estimated_delay_minutes is None


def test_sin_maniobra_conocida_no_estima() -> None:
    d = next_departures([_tres_cantos(slack=None)], {ENTRANTE: _vivo(ENTRANTE, 20)}, _q())[0]
    assert d.estimated_delay_minutes is None


def test_linea_excluida_no_estima() -> None:
    """Lineas cuyo emparejamiento no se ha validado (lista de configuracion)."""
    d = next_departures([_tres_cantos()], {ENTRANTE: _vivo(ENTRANTE, 20)},
                        _q(estimate_excluded_routes=frozenset({"C4"})))[0]
    assert d.estimated_delay_minutes is None, "C4b pertenece a la familia C4"


def test_enlace_interlineado_c9_desde_c8() -> None:
    """En Cercedilla un C8 que llega da la vuelta como C9 (familias distintas)."""
    entrante = f"{SERV}36330C8b"
    saliente = f"{SERV}36901C9"
    parada = ScheduledStop(saliente, "10:05:00", "C9", "Cotos", False, entrante, 5,
                           True, "12006")
    q = DepartureQuery(now_minutes=9 * 60 + 50, service_prefix=SERV, estimate_from_inbound=True)
    d = next_departures([parada], {entrante: _vivo(entrante, 7)}, q)[0]
    assert d.estimated_delay_minutes == 7 - (5 - MIN_PHYSICAL_TURNAROUND_MINUTES)
    assert d.route == "C9"


def test_vuelta_localizada_sin_retraso_publicado_no_se_estima() -> None:
    """El viaje ya esta en el feed (posicion) aunque sin retraso: manda el feed,
    no se mezcla un estimado con un tren localizado."""
    vivos = {ENTRANTE: _vivo(ENTRANTE, 20), SALIENTE: _vivo(SALIENTE, None)}
    d = next_departures([_tres_cantos()], vivos, _q())[0]
    assert d.located is True
    assert d.estimated_delay_minutes is None
