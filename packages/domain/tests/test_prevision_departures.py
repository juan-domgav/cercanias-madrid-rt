"""Contrato: la previsión solo rellena el hueco que deja el feed (RB-013).

La previsión de un tren se usa ÚNICAMENTE si ese viaje no está en el feed ni hay
otra fuente mejor (estimación por entrante, aviso de enlace, entrante suprimido).
No mueve la hora prevista ni el orden: solo cambia lo que se dice del retraso.
"""

from __future__ import annotations

from cercanias_domain.departures import (
    DELAY_SOURCE_INBOUND_ESTIMATE,
    DELAY_SOURCE_LIVE,
    DELAY_SOURCE_PREDICTED,
    REALTIME_NO_DATA,
    REALTIME_NOT_PUBLISHED,
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    next_departures,
)
from cercanias_domain.prevision import DAY_TYPE_LABORABLE, Prediction

SERV = "1019L"
ATOCHA = "18000"
TRIP_C1 = f"{SERV}19799C1"
TRIP_C5 = f"{SERV}35001C5"
AHORA = 8 * 60


def _parada(trip: str = TRIP_C1, hora: str = "08:20:00", route: str = "C1", **kw: object) -> ScheduledStop:
    base: dict[str, object] = {"origin_stop_id": "10000", "origin_departure_hhmmss": "07:40:00"}
    base.update(kw)
    return ScheduledStop(trip, hora, route, "Aeropuerto T4", False, **base)  # type: ignore[arg-type]


def _pred(tren: str = "19799", linea: str = "C1", mediana: float = 3.0, n: int = 12) -> Prediction:
    return Prediction(linea, tren, ATOCHA, DAY_TYPE_LABORABLE, mediana, n)


def _q(predicciones: dict[str, Prediction] | None = None, **kw: object) -> DepartureQuery:
    preds = predicciones if predicciones is not None else {"19799": _pred()}
    base: dict[str, object] = {"now_minutes": AHORA, "service_prefix": SERV, "predictions": preds}
    base.update(kw)
    return DepartureQuery(**base)  # type: ignore[arg-type]


def test_sin_dato_en_vivo_lleva_la_prevision_sin_mover_la_hora() -> None:
    (d,) = next_departures([_parada()], {}, _q())
    assert d.prediction == _pred()
    assert d.predicted_delay_minutes == 3
    assert d.delay_source == DELAY_SOURCE_PREDICTED
    # La hora, la cuenta atrás y el estado del feed NO cambian: no es un dato.
    assert d.expected_hhmm == "08:20" and d.minutes_until == 20
    assert d.delay_minutes is None and d.located is False
    assert d.realtime_status == REALTIME_NO_DATA
    assert d.shows_on_time is False


def test_no_publicado_conserva_su_estado_y_suma_la_prevision() -> None:
    (d,) = next_departures([_parada(origin_departure_hhmmss="08:15:00")], {}, _q(publish_window_minutes=10))
    assert d.not_published is True
    assert d.realtime_status == REALTIME_NOT_PUBLISHED
    assert d.predicted_delay_minutes == 3


def test_el_dato_en_vivo_siempre_manda() -> None:
    vivo = {TRIP_C1: LiveTrain(TRIP_C1, 0, "4", next_stop_id=ATOCHA, status="IN_TRANSIT_TO")}
    (d,) = next_departures([_parada()], vivo, _q())
    assert d.prediction is None
    assert d.delay_source == DELAY_SOURCE_LIVE
    assert d.shows_on_time is True


def test_en_el_feed_sin_retraso_publicado_tampoco_se_preve() -> None:
    vivo = {TRIP_C1: LiveTrain(TRIP_C1, None, None, next_stop_id=ATOCHA)}
    (d,) = next_departures([_parada()], vivo, _q())
    assert d.prediction is None


def test_en_cabecera_sin_salir_no_se_preve() -> None:
    # RB-009: el tren está en el feed (en su andén de origen); su caso tiene reglas propias.
    origen = _parada(origin_stop_id=ATOCHA, is_origin=True, origin_departure_hhmmss="08:20:00")
    vivo = {TRIP_C1: LiveTrain(TRIP_C1, 0, "4", next_stop_id=ATOCHA, status="STOPPED_AT")}
    (d,) = next_departures([origen], vivo, _q())
    assert d.pending_departure is True and d.prediction is None


def test_la_estimacion_por_entrante_manda_sobre_la_prevision() -> None:
    entrante = f"{SERV}19798C1"
    parada = _parada(is_origin=True, inbound_trip_id=entrante, turnaround_slack_minutes=5,
                     origin_stop_id=ATOCHA, origin_departure_hhmmss="08:20:00")
    vivo = {entrante: LiveTrain(entrante, 10 * 60, None, next_stop_id=ATOCHA)}
    (d,) = next_departures([parada], vivo, _q(estimate_from_inbound=True))
    assert d.estimated_delay_minutes is not None
    assert d.prediction is None
    assert d.delay_source == DELAY_SOURCE_INBOUND_ESTIMATE


def test_con_aviso_de_enlace_no_se_preve() -> None:
    entrante = f"{SERV}19798C1"
    parada = _parada(is_origin=True, inbound_trip_id=entrante, turnaround_slack_minutes=5,
                     origin_stop_id=ATOCHA, origin_departure_hhmmss="08:20:00")
    vivo = {entrante: LiveTrain(entrante, 25 * 60, None, next_stop_id=ATOCHA)}
    (d,) = next_departures([parada], vivo, _q())  # sin estimación: queda el aviso
    assert d.inbound_delay_minutes is not None
    assert d.prediction is None


def test_con_el_entrante_suprimido_no_se_preve() -> None:
    entrante = f"{SERV}19798C1"
    parada = _parada(is_origin=True, inbound_trip_id=entrante, origin_stop_id=ATOCHA,
                     origin_departure_hhmmss="08:20:00")
    vivo = {entrante: LiveTrain(entrante, None, None, cancelled=True)}
    (d,) = next_departures([parada], vivo, _q())
    assert d.inbound_cancelled is True and d.prediction is None


def test_la_prevision_tiene_que_ser_de_la_misma_linea() -> None:
    # Mismo número de tren en otra familia: no es el mismo tren.
    (d,) = next_departures([_parada()], {}, _q({"19799": _pred(linea="C5")}))
    assert d.prediction is None


def test_sin_previsiones_nada_cambia() -> None:
    (d,) = next_departures([_parada()], {}, DepartureQuery(now_minutes=AHORA, service_prefix=SERV))
    assert d.prediction is None and d.predicted_delay_minutes is None and d.delay_source is None


def test_cada_tren_su_prevision() -> None:
    paradas = [_parada(), _parada(TRIP_C5, "08:25:00", "C5")]
    preds = {"19799": _pred(), "35001": _pred("35001", "C5", 0.0, 7)}
    a, b = next_departures(paradas, {}, _q(preds))
    assert a.predicted_delay_minutes == 3
    assert b.predicted_delay_minutes == 0 and b.prediction is not None and b.prediction.observations == 7
