"""Contrato: un tren especial (SPECIAL_10_*) del feed llega al tablon. Ver RB-006.

Usa la pasada real del 21/10/2026 08:41:09 (Madrid), cuando el 91887 estaba
parado en Universidad Cantoblanco (17009) y boards.json no lo mostraba en
ninguna estacion. El feed entra por ``run_cycle`` igual que en produccion; solo
se sustituyen las descargas por los fixtures.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick, mvp
from cercanias_worker.config import WorkerConfig

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
AHORA = datetime(2026, 10, 21, 8, 41, 9, tzinfo=cloud_tick.MADRID_TZ)


class _Congelado(datetime):
    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
        return cls.fromtimestamp(AHORA.timestamp(), tz=tz)


def _snapshot_desde_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    payload = json.loads((FIXTURES / "vehicle_positions_especial_c4b.json").read_text())
    monkeypatch.setenv("RENFE_NUCLEO_CODES", "10")
    monkeypatch.setattr(mvp, "fetch_vehicle_positions_payload", lambda _url: payload)
    monkeypatch.setattr(mvp, "fetch_trip_updates_payload", lambda: {"entity": []})
    store = mvp.SnapshotStore(tmp_path / "snapshot.json")
    mvp.run_cycle(WorkerConfig(), store, InMemoryCurrentStateRepository())
    snapshot: dict[str, Any] = json.loads((tmp_path / "snapshot.json").read_text())
    return snapshot


def _boards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    snapshot = _snapshot_desde_fixture(monkeypatch, tmp_path)
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True)
    shutil.copy(FIXTURES / "horarios_1092X_c4_norte.json", web / "horarios" / "1092X.json")
    monkeypatch.setattr(cloud_tick, "datetime", _Congelado)
    return cloud_tick._build_boards(snapshot, LocalSnapshotPublisher(web))


def test_el_fixture_reproduce_la_incidencia(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Precondiciones: el especial pasa el filtro y NO existe en el horario."""
    snapshot = _snapshot_desde_fixture(monkeypatch, tmp_path)
    especial = next(v for v in snapshot["vehicles"] if v["trip_id"] == "SPECIAL_10_91887C4B")
    assert especial["next_stop_id"] == "17009"
    assert especial["status"] == "STOPPED_AT"
    assert especial["platform"] == "1"
    assert especial["delay_seconds"] is None
    horario = json.loads((FIXTURES / "horarios_1092X_c4_norte.json").read_text())
    viajes = {p["trip"] for paradas in horario["stations"].values() for p in paradas}
    assert not any("91887" in v or v.startswith("SPECIAL") for v in viajes)


def test_especial_parado_aparece_en_su_tablon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path)
    assert boards["service"] == "1092X", "los especiales no cuentan como dia de servicio"
    salidas = boards["stations"]["17009"]["departures"]
    (d,) = [s for s in salidas if s["train"] == "91887"]
    assert d["special"] is True
    assert d["live"] is True
    assert d["unconfirmed"] is False
    assert d["t"] is None, "sin hora de horario, no se inventa una"
    assert d["eta"] == "08:41"
    assert d["in"] == 0
    assert d["delay"] is None
    assert d["on_time"] is False
    assert d["platform"] == "1"
    assert d["route"] == "C4b"
    assert d["status"] == "STOPPED_AT"


def test_especial_en_camino_aparece_en_su_proxima_parada(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boards = _boards(monkeypatch, tmp_path)
    (d,) = [s for s in boards["stations"]["17000"]["departures"] if s["train"] == "91891"]
    assert d["status"] == "IN_TRANSIT_TO"
    assert d["live"] is True
    assert not any(s["train"] == "91891" for s in boards["stations"]["17009"]["departures"])


def test_el_casado_con_el_horario_no_cambia(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path)
    salidas = boards["stations"]["17009"]["departures"]
    programadas = [s for s in salidas if not s["special"]]
    assert all(s["t"] is not None for s in programadas)
    por_tren = {s["train"]: s for s in programadas}
    # 20431 (08:38) sigue sin rastro en el feed: sin confirmar, como antes.
    assert por_tren["20431"]["unconfirmed"] is True
    assert por_tren["20431"]["live"] is False
    # 20433 (08:48, Colmenar) esta en el feed y se casa por trip_id.
    assert por_tren["20433"]["live"] is True
    assert por_tren["20433"]["platform"] == "2"
    assert por_tren["20433"]["unconfirmed"] is False
