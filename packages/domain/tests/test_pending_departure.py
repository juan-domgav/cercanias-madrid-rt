"""RB-009 en el dominio: tren en el feed pero aún en su estación de origen."""

from __future__ import annotations

from cercanias_domain.board_status import contradicts_live, departure_status
from cercanias_domain.departures import DepartureQuery, LiveTrain, ScheduledStop, next_departures

SERV = "1093J"
COLMENAR, TRES_CANTOS = "17005", "17004"


def _origen() -> ScheduledStop:
    return ScheduledStop(f"{SERV}20430C4b", "08:43:00", "C4b", "Parla", False,
                         f"{SERV}20429C4b", 1, True, COLMENAR,
                         origin_departure_hhmmss="08:43:00")


def _intermedia() -> ScheduledStop:
    return ScheduledStop(f"{SERV}20430C4b", "08:49:00", "C4b", "Parla", False,
                         None, None, False, COLMENAR, origin_departure_hhmmss="08:43:00")


def _vivos(status: str = "STOPPED_AT", next_stop: str = COLMENAR, delay: int = 0) -> dict[str, LiveTrain]:
    return {
        f"{SERV}20430C4b": LiveTrain(f"{SERV}20430C4b", delay, "3", next_stop, status=status),
        f"{SERV}20429C4b": LiveTrain(f"{SERV}20429C4b", 660, "1", "17009", status="INCOMING_AT"),
    }


def _q(now: int, **kw: object) -> DepartureQuery:
    return DepartureQuery(now_minutes=now, service_prefix=SERV, **kw)  # type: ignore[arg-type]


def test_cero_antes_de_salir_no_es_medida() -> None:
    (d,) = next_departures([_origen()], _vivos(), _q(8 * 60 + 36))
    assert d.located and d.pending_departure
    assert d.delay_minutes is None and d.shows_on_time is False
    assert d.realtime_status == "pending_departure"
    assert d.inbound_delay_minutes is None, "C4 no esta en ORIGIN_ESTIMATE_ROUTES"


def test_retraso_heredado_antes_de_salir_tampoco_es_medida() -> None:
    """Renfe a veces publica en cabecera el retraso que arrastra la unidad: el 81 %
    de esos >0 salieron con < 3 min (archivo 19-22/10)."""
    (d,) = next_departures([_origen()], _vivos(delay=540), _q(8 * 60 + 36))
    assert d.delay_minutes is None and d.pending_departure


def test_pasada_la_hora_cuenta_lo_que_lleva_esperando() -> None:
    (d,) = next_departures([_origen()], _vivos(), _q(8 * 60 + 47))
    assert d.delay_minutes == 4 and d.expected_hhmm == "08:47" and d.pending_departure
    (d,) = next_departures([_origen()], _vivos(delay=900), _q(8 * 60 + 47))
    assert d.delay_minutes == 15, "si Renfe ya cuenta mas, se usa lo publicado"


def test_en_parada_intermedia_tambien_pendiente() -> None:
    (d,) = next_departures([_intermedia()], _vivos(), _q(8 * 60 + 40))
    assert d.pending_departure and d.delay_minutes is None


def test_una_vez_salido_manda_el_dato_en_vivo() -> None:
    """Al salir Renfe pasa a IN_TRANSIT_TO con next_stop = origen unos minutos."""
    for status, nxt in (("IN_TRANSIT_TO", COLMENAR), ("INCOMING_AT", TRES_CANTOS)):
        (d,) = next_departures([_origen()], _vivos(status, nxt), _q(8 * 60 + 44))
        assert not d.pending_departure and d.delay_minutes == 0 and d.shows_on_time


def test_estimacion_en_cabecera_solo_en_lineas_permitidas() -> None:
    q = {"estimate_from_inbound": True}
    (d,) = next_departures([_origen()], _vivos(), _q(8 * 60 + 36, **q))
    assert d.estimated_delay_minutes is None
    (d,) = next_departures([_origen()], _vivos(),
                           _q(8 * 60 + 36, origin_estimate_routes=frozenset({"C4"}), **q))
    assert d.estimated_delay_minutes == 13, "+11 con 1 min de maniobra: 11 - (1 - 3)"
    assert d.inbound_delay_minutes == 11
    assert d.pending_departure and d.delay_minutes is None


def test_un_tren_con_retraso_no_desaparece_a_los_tres_minutos() -> None:
    parada = ScheduledStop(f"{SERV}20429C4b", "08:33:00", "C4b", "Colmenar Viejo", False)
    vivos = {f"{SERV}20429C4b": LiveTrain(f"{SERV}20429C4b", 720, "2", TRES_CANTOS)}
    (d,) = next_departures([parada], vivos, _q(8 * 60 + 44))
    assert d.expected_hhmm == "08:45"
    assert next_departures([parada], vivos, _q(8 * 60 + 49)) == ()


def _fila(**kw: object) -> dict[str, object]:
    base: dict[str, object] = {"t": "08:43", "eta": "08:43", "route": "C4b", "dest": "Parla",
                               "train": "20430", "delay": None, "platform": "3", "live": True,
                               "pending_departure": True, "realtime_status": "pending_departure"}
    base.update(kw)
    return base


def test_presentacion_pendiente_de_salir() -> None:
    s = departure_status(_fila())
    assert (s.kind, s.label, s.tone, s.notices) == (
        "pending_departure", "pendiente de salir · Renfe aún no da retraso", "neutral", ())
    s = departure_status(_fila(delay=4))
    assert (s.label, s.show_scheduled) == ("+4 min · aún sin salir", True)
    s = departure_status(_fila(estimated_delay=9, delay_source="inbound_estimate", inbound_delay=11))
    assert s.label == "≈ +9 min (estimado)" and s.approximate
    assert [n.kind for n in s.notices] == ["estimated", "inbound_delay"]
    assert "posible retraso" in s.notices[1].text


def test_contradicts_live_coherente() -> None:
    assert contradicts_live(_fila(inbound_delay=11)) is False, "sin medida, no hay contradiccion"
    assert contradicts_live(_fila(delay=4, inbound_delay=11)) is True
    assert contradicts_live(_fila(unconfirmed=True)) is True
    assert contradicts_live(_fila(pending_departure=False, realtime_status="live", delay=0,
                                  inbound_delay=11)) is True
