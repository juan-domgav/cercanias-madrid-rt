"""Contrato: la vuelta en cabecera sin dato en vivo se estima desde su entrante.

Fixture real (21/10/2026, 15:26): 20479 (Parla -> Colmenar Viejo) pasa por Tres
Cantos con +6; 20476 (Colmenar 15:57 -> Parla) es su vuelta (14 min de
maniobra) y Renfe aun no lo publica. Ver RB-007.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
AHORA = datetime(2026, 10, 21, 15, 26, 30, tzinfo=cloud_tick.MADRID_TZ)
TRES_CANTOS = "17004"
COLMENAR = "17005"


@pytest.fixture(autouse=True)
def _estimar_en_todas(monkeypatch: pytest.MonkeyPatch) -> None:
    """El mecanismo se prueba con la lista de exclusion vacia; el valor por
    defecto (C4 excluida) tiene su propio test."""
    monkeypatch.setattr(cloud_tick, "INBOUND_ESTIMATE_EXCLUDED_ROUTES", frozenset())


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def _snapshot(*, delay_min: int = 6, edad_s: float = 30, **extra: Any) -> dict[str, Any]:
    snap: dict[str, Any] = json.loads((FIXTURES / "snapshot_vuelta_colmenar_20479.json").read_text())
    snap["generated_at"] = (AHORA - timedelta(seconds=edad_s)).isoformat()
    snap["vehicles"][0]["delay_seconds"] = delay_min * 60
    snap.update(extra)
    return snap


def _boards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "horarios_1092X_c4b_vuelta_colmenar.json", web / "horarios" / "1092X.json")
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    return cloud_tick._build_boards(snapshot, LocalSnapshotPublisher(web))


def _tren(boards: dict[str, Any], stop_id: str, numero: str) -> dict[str, Any]:
    return next(d for d in boards["stations"][stop_id]["departures"] if d["train"] == numero)


def test_caso_juan_vuelta_ausente_se_estima_en_tres_cantos(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boards = _boards(monkeypatch, tmp_path, _snapshot(delay_min=6))
    d = _tren(boards, TRES_CANTOS, "20476")
    assert d["live"] is False
    assert d["delay"] is None, "no es un dato en vivo"
    assert d["delay_source"] == "inbound_estimate"
    assert d["estimated_delay"] == 0, "+6 cabe en 14 min de maniobra (holgura 11)"
    assert d["on_time"] is False, "un estimado no se pinta como 'en hora' real"
    entrante = _tren(boards, TRES_CANTOS, "20479")
    assert entrante["delay_source"] == "live"
    assert entrante["estimated_delay"] is None


def test_entrante_muy_retrasado_desplaza_la_hora(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path, _snapshot(delay_min=20))
    d = _tren(boards, COLMENAR, "20476")
    assert d["estimated_delay"] == 9
    assert (d["t"], d["eta"]) == ("15:57", "16:06")
    assert boards["health"]["departures_estimated_from_inbound"] >= 2, "Colmenar y Tres Cantos, al menos"
    assert boards["health"]["inbound_estimates_disabled_stale"] is False


def test_snapshot_degradado_no_estima(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path, _snapshot(delay_min=20, degraded=True))
    d = _tren(boards, COLMENAR, "20476")
    assert d["delay_source"] is None
    assert d["eta"] == "15:57"
    assert boards["health"]["departures_estimated_from_inbound"] == 0
    assert boards["health"]["inbound_estimates_disabled_stale"] is True


def test_snapshot_viejo_no_estima(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    viejo = cloud_tick.STALE_WARNING_SECONDS + 60
    boards = _boards(monkeypatch, tmp_path, _snapshot(delay_min=20, edad_s=viejo))
    assert _tren(boards, COLMENAR, "20476")["delay_source"] is None


def test_linea_excluida_por_configuracion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cloud_tick, "INBOUND_ESTIMATE_EXCLUDED_ROUTES", frozenset({"C4"}))
    boards = _boards(monkeypatch, tmp_path, _snapshot(delay_min=20))
    assert _tren(boards, COLMENAR, "20476")["delay_source"] is None


def test_la_vuelta_en_el_feed_anula_la_estimacion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snap = _snapshot(delay_min=20)
    snap["vehicles"].append({"trip_id": "1092X20476C4b", "route": "C4b", "platform": "3",
                             "next_stop_id": COLMENAR, "status": "STOPPED_AT", "delay_seconds": 120})
    boards = _boards(monkeypatch, tmp_path, snap)
    d = _tren(boards, COLMENAR, "20476")
    assert (d["delay_source"], d["delay"], d["estimated_delay"]) == ("live", 2, None)


def test_enlace_ambiguo_en_el_horario_no_estima(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True)
    horario = json.loads((FIXTURES / "horarios_1092X_c4b_vuelta_colmenar.json").read_text())
    for paradas in horario["stations"].values():
        for p in paradas:
            if p["trip"] == "1092X20476C4b":
                p["amb"] = True
    (web / "horarios" / "1092X.json").write_text(json.dumps(horario))
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    boards = cloud_tick._build_boards(_snapshot(delay_min=20), LocalSnapshotPublisher(web))
    assert _tren(boards, COLMENAR, "20476")["delay_source"] is None


def test_por_defecto_solo_se_estima_en_lineas_validadas() -> None:
    """RB-007: la validacion del 21/10 solo respalda C1. En C4 (el caso de Tres
    Cantos) el tren que llega a Colmenar no es el que sale en mas de la mitad de
    los casos, y estimar seria mentir con aplomo."""
    excluidas = {r.strip() for r in cloud_tick.DEFAULT_INBOUND_ESTIMATE_EXCLUDED_ROUTES.split(",")}
    assert "C4" in excluidas
    assert "C1" not in excluidas


def test_linea_excluida_por_defecto_no_estima(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        cloud_tick, "INBOUND_ESTIMATE_EXCLUDED_ROUTES",
        frozenset(cloud_tick.DEFAULT_INBOUND_ESTIMATE_EXCLUDED_ROUTES.split(",")),
    )
    boards = _boards(monkeypatch, tmp_path, _snapshot(delay_min=20))
    assert _tren(boards, COLMENAR, "20476")["delay_source"] is None
