"""Contrato: la predicción (RB-021) sólo rellena huecos y nunca contradice al vivo."""

from __future__ import annotations

from typing import Any

from cercanias_domain.board_status import KIND_PREDICTED, departure_status
from cercanias_domain.departures import (
    DELAY_SOURCE_PREDICTED,
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    next_departures,
)

SERV = "1092X"
TRIP = f"{SERV}20942C5"
ENTRANTE = f"{SERV}20941C5"
AHORA = 15 * 60 + 30


def _stop(*, origen: bool = True, inbound: str | None = None, slack: int | None = None) -> ScheduledStop:
    return ScheduledStop(
        TRIP,
        "15:50:00",
        "C5",
        "Móstoles-El Soto",
        False,
        inbound,
        slack,
        origen,
        "35600",
        origin_departure_hhmmss="15:50:00",
        stop_id="35600",
        seq=1,
    )


class _Predictor:
    def __init__(self, valor: int | None = 4) -> None:
        self.valor = valor
        self.llamadas: list[str] = []

    def __call__(self, stop: ScheduledStop) -> int | None:
        self.llamadas.append(stop.trip_id)
        return self.valor


def _salida(live: dict[str, LiveTrain] | None = None, pred: _Predictor | None = None, **kw: Any) -> Any:
    q = DepartureQuery(now_minutes=AHORA, service_prefix=SERV, predictor=pred, **kw)
    return next_departures([_stop()], live or {}, q)[0]


def test_sin_predictor_todo_igual() -> None:
    d = _salida()
    assert d.predicted_delay_minutes is None
    assert d.delay_source is None


def test_fuera_del_feed_se_predice_sin_mover_la_hora() -> None:
    pred = _Predictor(4)
    d = _salida(pred=pred)
    assert (d.predicted_delay_minutes, d.delay_source) == (4, DELAY_SOURCE_PREDICTED)
    assert (d.expected_hhmm, d.minutes_until) == ("15:50", 20), "la previsión no mueve eta ni el orden"
    assert d.realtime_status == "no_data"
    assert pred.llamadas == [TRIP]


def test_en_vivo_no_se_llama_al_modelo() -> None:
    pred = _Predictor(9)
    vivo = {TRIP: LiveTrain(TRIP, 120, "2", "35601", status="IN_TRANSIT_TO")}
    d = _salida(vivo, pred)
    assert (d.delay_minutes, d.predicted_delay_minutes, d.delay_source) == (2, None, "live")
    assert pred.llamadas == []


def test_en_cabecera_antes_de_su_hora_si_se_predice() -> None:
    vivo = {TRIP: LiveTrain(TRIP, 0, "2", "35600", status="STOPPED_AT")}
    d = _salida(vivo, _Predictor(3))
    assert d.pending_departure and d.delay_minutes is None
    assert d.predicted_delay_minutes == 3


def test_en_cabecera_pasada_su_hora_manda_lo_que_lleva() -> None:
    vivo = {TRIP: LiveTrain(TRIP, 0, "2", "35600", status="STOPPED_AT")}
    q = DepartureQuery(now_minutes=15 * 60 + 54, service_prefix=SERV, predictor=_Predictor(1))
    d = next_departures([_stop()], vivo, q)[0]
    assert (d.delay_minutes, d.predicted_delay_minutes) == (4, None)


def test_estimado_o_aviso_del_entrante_mandan_sobre_el_modelo() -> None:
    pred = _Predictor(1)
    entrante = {ENTRANTE: LiveTrain(ENTRANTE, 25 * 60, None, "35600")}
    q = DepartureQuery(now_minutes=AHORA, service_prefix=SERV, predictor=pred, estimate_from_inbound=True)
    d = next_departures([_stop(inbound=ENTRANTE, slack=10)], entrante, q)[0]
    assert d.delay_source == "inbound_estimate" and d.predicted_delay_minutes is None
    suprimido = {ENTRANTE: LiveTrain(ENTRANTE, None, None, None, cancelled=True)}
    q2 = DepartureQuery(now_minutes=AHORA, service_prefix=SERV, predictor=pred)
    d2 = next_departures([_stop(inbound=ENTRANTE)], suprimido, q2)[0]
    assert d2.inbound_cancelled and d2.predicted_delay_minutes is None
    assert pred.llamadas == []


def test_suprimido_sin_posicion_no_se_predice() -> None:
    pred = _Predictor(5)
    d = _salida({TRIP: LiveTrain(TRIP, None, None, None, cancelled=True, positioned=False)}, pred)
    assert d.cancelled and d.predicted_delay_minutes is None
    assert pred.llamadas == []


def test_el_modelo_no_sabe() -> None:
    d = _salida(pred=_Predictor(None))
    assert d.predicted_delay_minutes is None and d.delay_source is None


# ------------------------------------------------------------- presentación


def _fila(**kw: Any) -> dict[str, Any]:
    base = {
        "t": "15:50",
        "eta": "15:50",
        "live": False,
        "delay": None,
        "estimated_delay": None,
        "delay_source": "predicted",
        "predicted_delay": 3,
        "realtime_status": "not_published",
    }
    return {**base, **kw}


def test_previsto_con_retraso() -> None:
    s = departure_status(_fila())
    assert (s.kind, s.label, s.tone, s.approximate, s.show_scheduled) == (
        KIND_PREDICTED,
        "previsto +3 min",
        "estimated",
        False,
        False,
    )
    assert [n.kind for n in s.notices] == ["predicted"]
    assert s.notices[0].icon == "📊"


def test_previsto_nunca_dice_en_hora() -> None:
    assert departure_status(_fila(predicted_delay=0)).label == "previsto sin retraso"
    assert departure_status(_fila(predicted_delay=-1)).label == "previsto sin retraso"


def test_previsto_con_sin_confirmar_conserva_el_aviso() -> None:
    s = departure_status(_fila(unconfirmed=True, realtime_status="no_data"))
    assert [n.kind for n in s.notices] == ["predicted", "unconfirmed"]


def test_previsto_en_cabecera_aun_sin_salir() -> None:
    fila = _fila(live=True, pending_departure=True, realtime_status="pending_departure")
    s = departure_status(fila)
    assert s.label == "previsto +3 min · aún sin salir"
    assert [n.kind for n in s.notices] == ["predicted"]
    assert departure_status({**fila, "predicted_delay": 0}).label == "previsto sin retraso · aún sin salir"


def test_el_vivo_y_el_estimado_mandan_sobre_una_fila_con_prevision() -> None:
    vivo = departure_status(_fila(live=True, delay=2, delay_source="live"))
    assert (vivo.kind, vivo.label) == ("live", "+2 min")
    est = departure_status(
        _fila(estimated_delay=5, delay_source="inbound_estimate", realtime_status="inbound_estimate")
    )
    assert est.kind == "inbound_estimate"
    sin = departure_status(_fila(predicted_delay=None))
    assert sin.kind == "not_published"
