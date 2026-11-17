"""Contrato: un viaje suprimido que sólo está en trip_updates (RB-019).

Renfe publica la supresión (``CANCELED``) en trip_updates, pero un tren que no
va a circular no tiene posición en vehicle_positions. El dominio lo recibe como
``LiveTrain(cancelled=True, positioned=False)``: suprimido sí, localizado no.
"""

from __future__ import annotations

from dataclasses import replace

from cercanias_domain.board_status import departure_status
from cercanias_domain.departures import (
    PUBLISH_WINDOW_MINUTES,
    REALTIME_CANCELLED,
    REALTIME_LIVE,
    DepartureQuery,
    LiveTrain,
    ScheduledStop,
    next_departures,
)

SERV = "1020M"
VIAJE = f"{SERV}20432C4b"      # Colmenar Viejo 08:55 -> Parla
ENTRANTE = f"{SERV}20425C4b"
AHORA = 8 * 60 + 54


def _parada() -> ScheduledStop:
    return ScheduledStop(VIAJE, "08:55:00", "C4b", "Parla", False, ENTRANTE, 34, True, "17005",
                         origin_departure_hhmmss="08:55:00")


def _q() -> DepartureQuery:
    return DepartureQuery(now_minutes=AHORA, service_prefix=SERV,
                          publish_window_minutes=PUBLISH_WINDOW_MINUTES)


def _suprimido() -> LiveTrain:
    return LiveTrain(VIAJE, None, None, cancelled=True, positioned=False)


def test_suprimido_sin_posicion_es_suprimido_y_no_localizado() -> None:
    d = next_departures([_parada()], {VIAJE: _suprimido()}, _q())[0]
    assert d.cancelled is True
    assert d.located is False
    assert d.unconfirmed is False
    assert d.not_published is False
    assert d.realtime_status == REALTIME_CANCELLED
    assert d.platform is None and d.delay_minutes is None


def test_se_pinta_suprimido() -> None:
    d = next_departures([_parada()], {VIAJE: _suprimido()}, _q())[0]
    fila = {"cancelled": d.cancelled, "live": d.located, "realtime_status": d.realtime_status,
            "delay": d.delay_minutes, "unconfirmed": d.unconfirmed, "t": d.scheduled_hhmm,
            "eta": d.expected_hhmm}
    estado = departure_status(fila)
    assert estado.label == "SUPRIMIDO"
    assert estado.tone == "cancelled"


def test_el_entrante_vivo_no_estima_un_suprimido() -> None:
    """Ni estimación desde el entrante ni aviso de enlace para un viaje suprimido."""
    live = {VIAJE: _suprimido(), ENTRANTE: LiveTrain(ENTRANTE, 40 * 60, "1", "17005")}
    d = next_departures([_parada()], live, replace(_q(), estimate_from_inbound=True))[0]
    assert d.estimated_delay_minutes is None
    assert d.inbound_delay_minutes is None
    assert d.cancelled is True


def test_suprimido_con_posicion_sigue_en_vivo() -> None:
    """Comportamiento previo intacto: con posición, ``live`` y SUPRIMIDO."""
    tren = LiveTrain(VIAJE, 0, "2", "17005", cancelled=True)
    d = next_departures([_parada()], {VIAJE: tren}, _q())[0]
    assert d.located is True
    assert d.realtime_status == REALTIME_LIVE
    assert d.cancelled is True
