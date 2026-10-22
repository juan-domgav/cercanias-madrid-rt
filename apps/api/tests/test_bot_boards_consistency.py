"""RB-008: el bot y los tablones (web/mini app) dicen LO MISMO de cada tren.

Reproduce con datos reales el 22/10/2026 08:36:12 en Colmenar Viejo: archivo
compacto de esa pasada (solo C4) + horarios/1093J.json de Colmenar. En
produccion el bot contesto "sin confirmar" + "viene con +17 min" para el 20428,
que estaba en el anden 3 con delay 0 y salio en hora.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
from cercanias_api.bot import BotContext, format_departure, handle_update

from cercanias_domain.board_status import departure_status
from cercanias_domain.station_view import Station
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
AHORA = datetime(2026, 10, 22, 8, 36, 12, tzinfo=cloud_tick.MADRID_TZ)
COLMENAR = Station("17005", "Colmenar Viejo", 40.6452, -3.7766)


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True)
    shutil.copy(FIXTURES / "horarios_1093J_colmenar_rb008.json", web / "horarios" / "1093J.json")
    snap = json.loads((FIXTURES / "snapshot_colmenar_20428_2026-10-22T0836.json").read_text())
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    # Escenario mas duro: SIN excluir C4. Aun asi, con el viaje en vivo no hay aviso.
    monkeypatch.setattr(cloud_tick, "INBOUND_ESTIMATE_EXCLUDED_ROUTES", frozenset())
    resultado: dict[str, Any] = cloud_tick._build_boards(snap, LocalSnapshotPublisher(web))
    return resultado


def _proximos(boards: dict[str, Any]) -> str:
    r = handle_update({"message": {"chat": {"id": 1}, "text": "/proximos Colmenar Viejo"}},
                      BotContext(stations=[COLMENAR], boards=boards))
    assert r.reply is not None
    return r.reply.text


def _fila(boards: dict[str, Any], tren: str) -> dict[str, Any]:
    return next(d for d in boards["stations"]["17005"]["departures"] if d["train"] == tren)


def test_20428_en_el_tablon_en_vivo_y_en_hora_sin_aviso(boards: dict[str, Any]) -> None:
    d = _fila(boards, "20428")
    assert (d["live"], d["delay"], d["platform"]) == (True, 0, "3")
    assert d["on_time"] is True
    assert d["inbound_delay"] is None, "el 20427 (+17) no contradice el dato en vivo"
    assert d["unconfirmed"] is False
    assert d["display"]["label"] == "en hora"
    assert d["display"]["notices"] == []
    assert boards["health"]["departures_contradicting_live"] == 0


def test_bot_colmenar_viejo_0836_no_contradice_ni_inventa(boards: dict[str, Any]) -> None:
    """RB-008 + RB-009: el 20428 ya ha salido (IN_TRANSIT_TO) y salio en hora; los
    20430/20432/20434 siguen STOPPED_AT en Colmenar antes de su hora y Renfe da 0,
    que no es una medida (salieron +8, +6, +8): "pendiente de salir", nunca "en hora"."""
    t = _proximos(boards)
    assert "sin confirmar" not in t
    assert "viene con" not in t
    assert "<b>08:33</b> → Parla\n   C4b tren 20428 · Andén <b>3</b> · en hora\n<b>08:43</b>" in t
    for tren in ("20430", "20432", "20434"):
        assert (
            f"C4b tren {tren} · Andén <b>3</b> · pendiente de salir · Renfe aún no da retraso" in t
        )


def test_bot_y_tablon_coinciden_en_cada_salida(boards: dict[str, Any]) -> None:
    """Para TODAS las salidas de TODAS las estaciones: lo que el bot pinta es
    exactamente el ``display`` que la ingesta publico para la web."""
    filas = [d for e in boards["stations"].values() for d in e["departures"]]
    assert filas
    for d in filas:
        cruda = {k: v for k, v in d.items() if k != "display"}
        assert departure_status(cruda).to_json() == d["display"], d["train"]
        texto = format_departure(d)
        assert f"· {d['display']['label']}" in texto
        for n in d["display"]["notices"]:
            if n["kind"] != "special":
                assert f"{n['icon']} {n['text']}" in texto
        if d["live"]:
            assert "sin confirmar" not in texto and "viene con" not in texto


def test_bot_nuevo_sobre_boards_de_main_tampoco_contradice(boards: dict[str, Any]) -> None:
    """Despliegue a medias (API nueva, ingesta vieja): una fila de main con live,
    delay 0, on_time False e inbound_delay 17 sale "en hora" y sin aviso."""
    vieja = {"t": "08:33", "eta": "08:33", "route": "C4b", "dest": "Parla", "train": "20428",
             "delay": 0, "platform": "3", "in": -3, "live": True, "inbound_delay": 17,
             "unconfirmed": False, "on_time": False, "cancelled": False,
             "inbound_cancelled": False, "shortened": False}
    t = format_departure(vieja)
    assert t == "<b>08:33</b> → Parla\n   C4b tren 20428 · Andén <b>3</b> · en hora"
