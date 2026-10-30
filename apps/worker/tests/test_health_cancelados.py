"""RB-015: ``delay_coverage`` del snapshot excluye los viajes suprimidos.

El 29/10/2026 el snapshot dio ``delay_coverage: critical`` (52/65) con los 13
viajes sin retraso todos CANCELED. Se prueba por ``run_cycle``, igual que en
produccion, para cubrir el cableado conector → health → snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_worker import mvp
from cercanias_worker.config import WorkerConfig

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _tu(en_marcha: int, sin_retraso: int, suprimidos: int) -> dict[str, Any]:
    entidades: list[dict[str, Any]] = []
    for n in range(en_marcha):
        entidades.append({"id": f"r{n}", "tripUpdate": {"trip": {"tripId": f"1080J1{n:04d}C1"}, "delay": 60}})
    for n in range(sin_retraso):
        # Acortado (SKIPPED) y sin retraso: sigue circulando, cuenta.
        entidades.append({"id": f"s{n}", "tripUpdate": {
            "trip": {"tripId": f"1080J2{n:04d}C3"},
            "stopTimeUpdate": [{"stopId": "17004", "scheduleRelationship": "SKIPPED"}],
        }})
    for n in range(suprimidos):
        entidades.append({"id": f"c{n}", "tripUpdate": {"trip": {
            "tripId": f"1080J3{n:04d}C4a", "scheduleRelationship": "CANCELED"}}})
    return {"entity": entidades}


def _ciclo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, tu: dict[str, Any]) -> dict[str, Any]:
    vp = json.loads((FIXTURES / "vehicle_positions_madrid.json").read_text())
    monkeypatch.setattr(mvp, "fetch_vehicle_positions_payload", lambda _url: vp)
    monkeypatch.setattr(mvp, "fetch_trip_updates_payload", lambda: tu)
    store = mvp.SnapshotStore(tmp_path / "s.json")
    mvp.run_cycle(WorkerConfig(), store, InMemoryCurrentStateRepository())
    snapshot: dict[str, Any] = json.loads((tmp_path / "s.json").read_text())
    cobertura: dict[str, Any] = snapshot["health"]["delay_coverage"]
    return cobertura


def test_snapshot_52_de_65_con_13_suprimidos_es_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cobertura = _ciclo(monkeypatch, tmp_path, _tu(en_marcha=52, sin_retraso=0, suprimidos=13))
    assert cobertura["severity"] == "ok", cobertura["detail"]
    assert cobertura["n_cancelados"] == 13
    assert "52/52" in cobertura["detail"]


def test_snapshot_acortados_sin_retraso_si_cuentan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cobertura = _ciclo(monkeypatch, tmp_path, _tu(en_marcha=40, sin_retraso=12, suprimidos=13))
    assert cobertura["severity"] == "critical"
    assert "40/52" in cobertura["detail"]
    assert cobertura["n_cancelados"] == 13
