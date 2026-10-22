"""Contrato: un viaje que Renfe aun no publica no es una anomalia (RB-007).

Renfe publica cada viaje ~30 min antes de su salida de CABECERA. Hasta entonces
el tablon dice "programado · Renfe aun no lo publica", no "sin datos en vivo".
La ventana se mide desde la salida de ORIGEN, no desde la hora en la estacion.
"""

from __future__ import annotations

from dataclasses import replace

from cercanias_domain.departures import (
    PUBLISH_WINDOW_MINUTES,
    REALTIME_INBOUND_ESTIMATE,
    REALTIME_LIVE,
    REALTIME_NO_DATA,
    REALTIME_NOT_PUBLISHED,
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    next_departures,
)

SERV = "1092X"
VUELTA = f"{SERV}20478C4b"     # sale de Colmenar 16:12, pasa por Tres Cantos 16:18
ENTRANTE = f"{SERV}20481C4b"
ORIGEN = "16:12:00"


def _parada(origen: str | None = ORIGEN, t: str = "16:18:00") -> ScheduledStop:
    return ScheduledStop(VUELTA, t, "C4b", "Parla", False, ENTRANTE, 8, False, "17004",
                         origin_departure_hhmmss=origen)


def _q(now: int, **kw: object) -> DepartureQuery:
    base: dict[str, object] = {"now_minutes": now, "service_prefix": SERV,
                               "publish_window_minutes": PUBLISH_WINDOW_MINUTES}
    base.update(kw)
    return DepartureQuery(**base)  # type: ignore[arg-type]


def _uno(stop: ScheduledStop, live: dict[str, LiveTrain], q: DepartureQuery) -> object:
    return next_departures([stop], live, q)[0]


ORIGEN_MIN = 16 * 60 + 12


def test_ventana_por_defecto_es_30() -> None:
    assert PUBLISH_WINDOW_MINUTES == 30


def test_fuera_de_la_ventana_no_publicado() -> None:
    d = next_departures([_parada()], {}, _q(ORIGEN_MIN - 31))[0]
    assert d.realtime_status == REALTIME_NOT_PUBLISHED
    assert d.unconfirmed is False


def test_justo_en_la_ventana_ya_deberia_estar() -> None:
    """A 30 min exactos Renfe ya lo publica: si falta, es 'sin datos'."""
    d = next_departures([_parada()], {}, _q(ORIGEN_MIN - 30))[0]
    assert d.realtime_status == REALTIME_NO_DATA


def test_se_mide_desde_el_origen_no_desde_la_estacion() -> None:
    """Tres Cantos 16:18 esta a 31 min, pero el origen (16:12) a 25: ya deberia
    estar publicado."""
    now = 16 * 60 + 18 - 31
    d = next_departures([_parada()], {}, _q(now))[0]
    assert d.realtime_status == REALTIME_NO_DATA
    # Y al reves: estacion a 29 min, origen a 31 -> aun no publicado.
    lejos = _parada(origen="16:12:00", t="16:14:00")
    d2 = next_departures([lejos], {}, _q(ORIGEN_MIN - 31))[0]
    assert d2.realtime_status == REALTIME_NOT_PUBLISHED


def test_sin_hora_de_origen_no_se_afirma() -> None:
    d = next_departures([_parada(origen=None)], {}, _q(ORIGEN_MIN - 60))[0]
    assert d.realtime_status == REALTIME_NO_DATA


def test_feed_viejo_no_afirma_no_publicado() -> None:
    d = next_departures([_parada()], {}, _q(ORIGEN_MIN - 60, publish_window_minutes=None))[0]
    assert d.realtime_status == REALTIME_NO_DATA


def test_el_dato_real_siempre_manda() -> None:
    vivos = {VUELTA: LiveTrain(VUELTA, None, None)}
    d = next_departures([_parada()], vivos, _q(ORIGEN_MIN - 60))[0]
    assert d.realtime_status == REALTIME_LIVE


def test_el_estimado_manda_sobre_no_publicado() -> None:
    vivos = {ENTRANTE: LiveTrain(ENTRANTE, 600, None)}
    q = _q(ORIGEN_MIN - 60, estimate_from_inbound=True)
    d = next_departures([_parada()], vivos, q)[0]
    assert d.realtime_status == REALTIME_INBOUND_ESTIMATE


def test_no_publicado_es_falso_si_hay_dato_o_estimado() -> None:
    """La precedencia se cumple en el propio campo, no solo en realtime_status:
    la web y el bot leen el JSON."""
    vivo = next_departures([_parada()], {VUELTA: LiveTrain(VUELTA, None, None)}, _q(ORIGEN_MIN - 60))[0]
    assert vivo.not_published is False
    q = _q(ORIGEN_MIN - 60, estimate_from_inbound=True)
    est = next_departures([_parada()], {ENTRANTE: LiveTrain(ENTRANTE, 600, None)}, q)[0]
    assert est.not_published is False


def test_precedencia_de_realtime_status() -> None:
    base = next_departures([_parada()], {}, _q(ORIGEN_MIN - 60))[0]
    assert replace(base, located=True).realtime_status == REALTIME_LIVE
    assert replace(base, estimated_delay_minutes=2).realtime_status == REALTIME_INBOUND_ESTIMATE
    assert replace(base, located=True, estimated_delay_minutes=2).realtime_status == REALTIME_LIVE
