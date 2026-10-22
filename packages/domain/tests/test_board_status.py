"""RB-008: una sola precedencia de estado para bot, web y mini app.

Caso real (22/10/2026, 08:36, Colmenar Viejo): el 20428 estaba en el feed con
delay 0 en el anden 3 y salio en hora (Renfe lo confirma), pero el bot dijo
"sin confirmar" + "el tren que hace este servicio viene con +17 min" porque el
20427 —que NO lo formaba— iba +17 por Tres Cantos.
"""

from __future__ import annotations

from typing import Any

from cercanias_domain.board_status import contradicts_live, departure_status
from cercanias_domain.departures import DepartureQuery, LiveTrain, ScheduledStop, next_departures

SERV = "1093J"
COLMENAR = "17005"
C4B_C1_EXCLUIDAS = frozenset({"C2", "C3", "C4", "C5", "C7", "C8", "C9", "C10"})


def _20428() -> ScheduledStop:
    # Horario real: 08:33 origen, "from" 20427, maniobra 4 min.
    return ScheduledStop(f"{SERV}20428C4b", "08:33:00", "C4b", "Parla", False,
                         f"{SERV}20427C4b", 4, True, COLMENAR)


def _vivos(propio: bool = True) -> dict[str, LiveTrain]:
    v = {f"{SERV}20427C4b": LiveTrain(f"{SERV}20427C4b", 1020, "2", "17004", status="STOPPED_AT")}
    if propio:
        v[f"{SERV}20428C4b"] = LiveTrain(f"{SERV}20428C4b", 0, "3", COLMENAR, status="IN_TRANSIT_TO")
    return v


def _q(excluidas: frozenset[str] = frozenset()) -> DepartureQuery:
    return DepartureQuery(now_minutes=8 * 60 + 36, service_prefix=SERV,
                          estimate_excluded_routes=excluidas)


def _fila(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"t": "08:33", "eta": "08:33", "route": "C4b", "dest": "Parla",
                            "train": "20428", "delay": 0, "platform": "3", "live": True}
    base.update(kw)
    return base


# --- dominio: el aviso del entrante no puede contradecir el dato en vivo -------


def test_20428_en_vivo_y_en_hora_sin_aviso_aunque_el_entrante_vaya_mas_17() -> None:
    """Incluso SIN excluir C4: estar en el feed basta para que no haya aviso."""
    (d,) = next_departures([_20428()], _vivos(), _q())
    assert d.located is True
    assert d.delay_minutes == 0
    assert d.inbound_delay_minutes is None
    assert d.unconfirmed is False
    assert d.shows_on_time is True
    assert d.realtime_status == "live"


def test_c4b_sin_dato_propio_tampoco_avisa_si_la_linea_esta_excluida() -> None:
    """C4b empareja bien solo el 47 %: con la config por defecto, no se avisa."""
    (d,) = next_departures([_20428()], _vivos(propio=False), _q(C4B_C1_EXCLUIDAS))
    assert d.inbound_delay_minutes is None


def test_el_mecanismo_sigue_vivo_donde_el_emparejamiento_es_fiable() -> None:
    """C1 (91 %) sin dato propio: se avisa y se estima, y nunca 'sin confirmar'."""
    parada = ScheduledStop(f"{SERV}25001C1", "08:40:00", "C1", "Aeropuerto T4", False,
                           f"{SERV}25000C1", 4, True, "18000")
    vivos = {f"{SERV}25000C1": LiveTrain(f"{SERV}25000C1", 900, "1", "18000")}
    q = DepartureQuery(now_minutes=8 * 60 + 36, service_prefix=SERV, estimate_from_inbound=True,
                       estimate_excluded_routes=C4B_C1_EXCLUIDAS)
    (d,) = next_departures([parada], vivos, q)
    assert d.located is False
    assert d.inbound_delay_minutes == 15
    assert d.estimated_delay_minutes == 14
    assert d.unconfirmed is False


def test_emparejamiento_ambiguo_no_avisa() -> None:
    parada = ScheduledStop(f"{SERV}25001C1", "08:40:00", "C1", "Aeropuerto T4", False,
                           f"{SERV}25000C1", 0, True, "18000", inbound_ambiguous=True)
    vivos = {f"{SERV}25000C1": LiveTrain(f"{SERV}25000C1", 900, "1", "18000")}
    (d,) = next_departures([parada], vivos, _q())
    assert d.inbound_delay_minutes is None


def test_entrante_suprimido_no_desmiente_un_viaje_en_vivo() -> None:
    vivos = _vivos()
    vivos[f"{SERV}20427C4b"] = LiveTrain(f"{SERV}20427C4b", 0, None, "17004", cancelled=True)
    (d,) = next_departures([_20428()], vivos, _q())
    assert d.inbound_cancelled is False
    assert d.shows_on_time is True


# --- funcion compartida de presentacion ----------------------------------------


def test_fila_del_22_10_tal_cual_la_publico_main_se_pinta_en_hora_sin_avisos() -> None:
    """La fila exacta de boards.json de produccion (ingesta de main): live, 0,
    on_time False por el aviso de +17. La funcion compartida la pinta bien aunque
    la ingesta no este actualizada: el dato en vivo manda."""
    fila = _fila(on_time=False, inbound_delay=17, unconfirmed=False)
    s = departure_status(fila)
    assert s.kind == "live"
    assert s.label == "en hora"
    assert s.tone == "ontime"
    assert s.notices == ()
    assert contradicts_live(fila) is True, "la fila cruda SI era contradictoria"


def test_en_vivo_nunca_sin_confirmar() -> None:
    for fila in (_fila(unconfirmed=True), _fila(delay=-1), _fila(delay=0, on_time=False),
                 _fila(inbound_cancelled=True)):
        s = departure_status(fila)
        assert "sin confirmar" not in s.label
        assert all(n.kind not in {"unconfirmed", "inbound_delay", "inbound_cancelled"}
                   for n in s.notices)


def test_en_vivo_con_retraso_y_sin_retraso_publicado() -> None:
    assert departure_status(_fila(delay=5)).label == "+5 min"
    assert departure_status(_fila(delay=5)).tone == "late"
    assert departure_status(_fila(delay=2)).tone == "ontime"
    assert departure_status(_fila(delay=5)).show_scheduled is True
    s = departure_status(_fila(delay=None))
    assert s.kind == "live" and "sin datos en vivo" not in s.label


def test_sin_dato_propio_si_se_conservan_los_avisos() -> None:
    fila = _fila(live=False, delay=None, platform=None, inbound_delay=12)
    assert [n.kind for n in departure_status(fila).notices] == ["inbound_delay"]
    fila = _fila(live=False, delay=None, platform=None, unconfirmed=True)
    assert [n.kind for n in departure_status(fila).notices] == ["unconfirmed"]
    assert departure_status(fila).label == "sin datos en vivo"


def test_precedencia_suprimido_estimado_no_publicado() -> None:
    assert departure_status(_fila(cancelled=True)).label == "SUPRIMIDO"
    est = departure_status(_fila(live=False, delay=None, delay_source="inbound_estimate",
                                 estimated_delay=4))
    assert (est.kind, est.label, est.approximate) == ("inbound_estimate", "≈ +4 min (estimado)", True)
    np = departure_status(_fila(live=False, delay=None, realtime_status="not_published"))
    assert np.label == "programado · Renfe aún no lo publica"
