"""Contrato: predicción LightGBM en los tablones (RB-021), apagada por defecto.

Horario sintético de C5 (21/10/2026, servicio 1092X): el 20941 llega a la
cabecera T a las 15:40 y su material hace el 20942 (T 15:50 -> A). A las 15:26
el 20941 va por B con +8 y el 20942 aún no está en el feed: sólo el modelo del
entrante (árbol de un corte: +8 > 5 -> +7) puede decir algo de él.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from cercanias_domain import prediccion as P
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

AHORA = datetime(2026, 10, 21, 15, 26, 30, tzinfo=cloud_tick.MADRID_TZ)
SERV = "1092X"
ENTRANTE = f"{SERV}20941C5"
SALIENTE = f"{SERV}20942C5"


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def _p(trip: str, t: str, seq: int, dest: str, *, final: bool = False) -> dict[str, Any]:
    return {
        "trip": trip,
        "t": f"{t}:00",
        "seq": seq,
        "dest": dest,
        "route": "C5",
        "terminates": final,
        "from": None,
        "slack": None,
    }


HORARIO = {
    "service": SERV,
    "stations": {
        "A": [_p(ENTRANTE, "15:00", 1, "T"), _p(SALIENTE, "16:30", 3, "A", final=True)],
        "B": [_p(ENTRANTE, "15:20", 2, "T"), _p(SALIENTE, "16:10", 2, "A")],
        "T": [_p(ENTRANTE, "15:40", 3, "T", final=True), _p(SALIENTE, "15:50", 1, "A")],
    },
}


def _artefacto(*, creado: datetime | None = None, lineas: dict[str, Any] | None = None) -> dict[str, Any]:
    rasgos = P.RASGOS_POR_MODELO[P.MODELO_ENTRANTE_VIVO]
    arbol = {
        "f": [rasgos.index("inc_live_delay")],
        "t": [5.0],
        "c": [None],
        "m": [0],
        "d": [False],
        "l": [-1],
        "r": [-2],
        "v": [1.0, 7.0],
    }
    return {
        "format": P.FORMATO,
        "version": "2026-10-20-test",
        "created_at": (creado or AHORA - timedelta(days=1)).isoformat(),
        "lines": lineas or {"C5": {"model": P.MODELO_ENTRANTE_VIVO, "max_horizon_min": 60}},
        "models": {
            P.MODELO_ENTRANTE_VIVO: {
                "features": list(rasgos),
                "categories": {c: [] for c in P.RASGOS_CATEGORICOS},
                "trees": [arbol],
            }
        },
        "stats": {"tren_parada": [], "franja": [], "tren": []},
        "holidays": [],
        "excluded_days": ["2026-10-29"],
    }


def _snapshot(*, edad_s: float = 30, delay_min: int = 8) -> dict[str, Any]:
    return {
        "generated_at": (AHORA - timedelta(seconds=edad_s)).isoformat(),
        "degraded": False,
        "vehicles": [
            {
                "trip_id": ENTRANTE,
                "route": "C5",
                "platform": "1",
                "next_stop_id": "B",
                "status": "IN_TRANSIT_TO",
                "delay_seconds": delay_min * 60,
            }
        ],
    }


def _boards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    rutas: str = "C5",
    artefacto: bytes | None = None,
    snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    web, privado = tmp_path / "web", tmp_path / "privado"
    (web / "horarios").mkdir(parents=True, exist_ok=True)
    (web / "horarios" / f"{SERV}.json").write_text(json.dumps(HORARIO))
    privado.mkdir(exist_ok=True)
    if artefacto is not None:
        (privado / "prediccion").mkdir(exist_ok=True)
        (privado / "prediccion" / "modelo.json").write_bytes(artefacto)
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    monkeypatch.setattr(cloud_tick, "PREDICCION_ROUTES", frozenset(r for r in rutas.split(",") if r))
    return cloud_tick._build_boards(
        snapshot or _snapshot(), LocalSnapshotPublisher(web), estado=LocalSnapshotPublisher(privado)
    )


def _fila(boards: dict[str, Any], stop: str, tren: str) -> dict[str, Any]:
    return next(d for d in boards["stations"][stop]["departures"] if d["train"] == tren)


def _ok() -> bytes:
    return gzip.compress(json.dumps(_artefacto()).encode())


def test_apagada_por_defecto_el_tablon_no_cambia_y_no_se_toca_nada(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert frozenset() == cloud_tick.PREDICCION_ROUTES, "por defecto: vacía = apagada"
    boards = _boards(monkeypatch, tmp_path, rutas="", artefacto=_ok())
    fila = _fila(boards, "B", "20942")
    assert "predicted_delay" not in fila
    assert fila["delay_source"] is None
    assert "prediction" not in boards["health"] and "departures_predicted" not in boards["health"]
    assert not (tmp_path / "privado" / "prediccion" / "observaciones.json").exists(), "no escribe nada"


def test_encendida_predice_el_saliente_sin_mover_la_hora(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boards = _boards(monkeypatch, tmp_path, artefacto=_ok())
    fila = _fila(boards, "B", "20942")
    assert (fila["delay_source"], fila["predicted_delay"]) == ("predicted", 7)
    assert (fila["t"], fila["eta"]) == ("16:10", "16:10")
    assert fila["display"]["label"] == "previsto +7 min"
    assert fila["display"]["notices"][0]["kind"] == "predicted"
    entrante = _fila(boards, "B", "20941")
    assert entrante["delay_source"] == "live" and entrante.get("predicted_delay") is None
    salud = boards["health"]
    assert salud["prediction"] == {
        "routes": ["C5"],
        "status": "ok",
        "artifact_version": "2026-10-20-test",
        "observations": 1,
    }
    assert salud["departures_predicted"] == 2, "en T y en B (A es su final)"
    obs = json.loads((tmp_path / "privado" / "prediccion" / "observaciones.json").read_text())
    assert [o[:3] for o in obs["obs"]] == [[ENTRANTE, "B", 480]]


def test_las_observaciones_se_acumulan_entre_ticks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _boards(monkeypatch, tmp_path, artefacto=_ok())
    snap = _snapshot(delay_min=2)
    snap["vehicles"][0]["next_stop_id"] = "T"
    boards = _boards(monkeypatch, tmp_path, artefacto=_ok(), snapshot=snap)
    assert boards["health"]["prediction"]["observations"] == 2
    assert _fila(boards, "B", "20942")["predicted_delay"] == 1, "manda el último dato: +2 <= 5 -> +1"


def test_sin_artefacto_no_predice_y_lo_grita(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    boards = _boards(monkeypatch, tmp_path)
    assert _fila(boards, "B", "20942").get("predicted_delay") is None
    assert boards["health"]["prediction"]["status"] == "artifact_missing"
    assert boards["health"]["departures_predicted"] == 0
    assert "prediction_artifact_missing" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("crudo", "estado", "alerta"),
    [
        (b"{no es json", "artifact_invalid", "prediction_artifact_invalid"),
        (
            json.dumps({**_artefacto(), "format": "otro"}).encode(),
            "artifact_invalid",
            "prediction_artifact_invalid",
        ),
        (
            json.dumps(_artefacto(creado=AHORA - timedelta(days=30))).encode(),
            "artifact_expired",
            "prediction_artifact_expired",
        ),
    ],
)
def test_artefacto_roto_o_caducado_falla_cerrado(  # noqa: PLR0913, PLR0917 - fixtures + parametrize
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    crudo: bytes,
    estado: str,
    alerta: str,
) -> None:
    boards = _boards(monkeypatch, tmp_path, artefacto=crudo)
    assert _fila(boards, "B", "20942")["delay_source"] is None
    assert boards["health"]["prediction"]["status"] == estado
    assert alerta in capsys.readouterr().err


def test_snapshot_viejo_no_predice(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    viejo = _snapshot(edad_s=cloud_tick.STALE_WARNING_SECONDS + 60)
    boards = _boards(monkeypatch, tmp_path, artefacto=_ok(), snapshot=viejo)
    assert _fila(boards, "B", "20942").get("predicted_delay") is None
    assert boards["health"]["prediction"]["status"] == "disabled_stale"


def test_linea_habilitada_que_el_artefacto_no_trae(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path, rutas="C1", artefacto=_ok())
    assert boards["health"]["prediction"]["status"] == "no_routes_in_artifact"
    assert _fila(boards, "B", "20942").get("predicted_delay") is None


def test_un_fallo_del_modelo_no_tumba_los_tablones(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def _roto(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("bug")

    monkeypatch.setattr(cloud_tick, "_build_predictor", _roto)
    boards = _boards(monkeypatch, tmp_path, artefacto=_ok())
    assert boards["health"]["prediction"]["status"] == "failed"
    assert _fila(boards, "B", "20942").get("predicted_delay") is None
    assert "prediction_failed" in capsys.readouterr().err


def test_observaciones_ilegibles_se_reinician(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / "privado" / "prediccion").mkdir(parents=True)
    (tmp_path / "privado" / "prediccion" / "observaciones.json").write_text("{roto")
    boards = _boards(monkeypatch, tmp_path, artefacto=_ok())
    assert boards["health"]["prediction"]["observations"] == 1


def test_no_se_observan_especiales_suprimidos_ni_trenes_en_origen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snap = _snapshot()
    snap["vehicles"] += [
        {"trip_id": SALIENTE, "next_stop_id": "T", "status": "STOPPED_AT", "delay_seconds": 0},
        {"trip_id": "SPECIAL_10_91887C5", "next_stop_id": "B", "status": "STOPPED_AT", "delay_seconds": 60},
        {"trip_id": f"{SERV}21001C7", "next_stop_id": "B", "status": "STOPPED_AT", "delay_seconds": 60},
        {"trip_id": f"{SERV}20946C5", "next_stop_id": "B", "status": "STOPPED_AT", "delay_seconds": None},
    ]
    boards = _boards(monkeypatch, tmp_path, artefacto=_ok(), snapshot=snap)
    assert boards["health"]["prediction"]["observations"] == 1
    fila = _fila(boards, "T", "20942")
    assert fila["pending_departure"] is True
    assert fila["display"]["label"] == "previsto +7 min · aún sin salir"
