"""Contrato: un tren especial (SPECIAL_10_*) del feed llega al tablon. Ver RB-006.

Usa pasadas reales del 21/10/2026 08:41:09 (Madrid), cuando el 91887 estaba
parado en Universidad Cantoblanco (17009) y boards.json no lo mostraba en
ninguna estacion. El feed entra por ``run_cycle`` igual que en produccion; solo
se sustituyen las descargas por los fixtures y se congela el reloj.
"""

from __future__ import annotations

import copy
import json
import shutil
from datetime import datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest

from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick, mvp
from cercanias_worker.config import WorkerConfig

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
VISTO = datetime(2026, 10, 21, 8, 41, 9, tzinfo=cloud_tick.MADRID_TZ)
FEED_INCIDENCIA = "vehicle_positions_especial_c4b.json"
FEED_MAYORIA = "vehicle_positions_especiales_mayoria.json"


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def _snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, feed: str = FEED_INCIDENCIA
) -> dict[str, Any]:
    payload = json.loads((FIXTURES / feed).read_text())
    monkeypatch.setenv("RENFE_NUCLEO_CODES", "10")
    monkeypatch.setattr(mvp, "fetch_vehicle_positions_payload", lambda _url: payload)
    monkeypatch.setattr(mvp, "fetch_trip_updates_payload", lambda: {"entity": []})
    monkeypatch.setattr(mvp, "datetime", _reloj(VISTO))
    store = mvp.SnapshotStore(tmp_path / "snapshot.json")
    mvp.run_cycle(WorkerConfig(), store, InMemoryCurrentStateRepository())
    snapshot: dict[str, Any] = json.loads((tmp_path / "snapshot.json").read_text())
    return snapshot


def _boards(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    snapshot: dict[str, Any] | None = None,
    *,
    ahora: datetime = VISTO,
    con_horario: bool = True,
) -> dict[str, Any]:
    if snapshot is None:
        snapshot = _snapshot(monkeypatch, tmp_path)
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True, exist_ok=True)
    if con_horario:
        shutil.copy(FIXTURES / "horarios_1092X_c4_norte.json", web / "horarios" / "1092X.json")
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(ahora))
    return cloud_tick._build_boards(snapshot, LocalSnapshotPublisher(web))


def _trenes(boards: dict[str, Any], stop_id: str) -> dict[str, dict[str, Any]]:
    return {d["train"]: d for d in boards["stations"].get(stop_id, {}).get("departures", [])}


# --- Precondiciones ------------------------------------------------------------


def test_el_fixture_reproduce_la_incidencia(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """El especial pasa el filtro y NO existe en el horario."""
    snapshot = _snapshot(monkeypatch, tmp_path)
    especial = next(v for v in snapshot["vehicles"] if v["trip_id"] == "SPECIAL_10_91887C4B")
    assert especial["next_stop_id"] == "17009"
    assert especial["status"] == "STOPPED_AT"
    assert especial["platform"] == "1"
    assert especial["delay_seconds"] is None
    horario = json.loads((FIXTURES / "horarios_1092X_c4_norte.json").read_text())
    viajes = {p["trip"] for paradas in horario["stations"].values() for p in paradas}
    assert not any("91887" in v or v.startswith("SPECIAL") for v in viajes)
    assert "18002" not in horario["stations"]


# --- El especial aparece ---------------------------------------------------------


def test_especial_parado_aparece_en_su_tablon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path)
    assert boards["service"] == "1092X", "los especiales no cuentan como dia de servicio"
    d = _trenes(boards, "17009")["91887"]
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
    assert d["at_terminus"] is False


def test_especial_en_camino_aparece_en_su_proxima_parada(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boards = _boards(monkeypatch, tmp_path)
    d = _trenes(boards, "17000")["91891"]
    assert d["status"] == "IN_TRANSIT_TO"
    assert d["live"] is True
    assert "91891" not in _trenes(boards, "17009")


def test_el_casado_con_el_horario_no_cambia(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path)
    programadas = {k: v for k, v in _trenes(boards, "17009").items() if not v["special"]}
    assert all(s["t"] is not None for s in programadas.values())
    # 20431 (08:38) sigue sin rastro en el feed: sin confirmar, como antes.
    assert programadas["20431"]["unconfirmed"] is True
    assert programadas["20431"]["live"] is False
    # 20433 (08:48, Colmenar) esta en el feed y se casa por trip_id.
    assert programadas["20433"]["live"] is True
    assert programadas["20433"]["platform"] == "2"
    assert programadas["20433"]["unconfirmed"] is False


# --- Dia de servicio con muchos especiales (mutacion A) --------------------------


def test_especiales_mayoritarios_no_se_eligen_como_dia_de_servicio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """3 especiales frente a 2 viajes 1092X: contar "SPECI" elegia ese dia."""
    snapshot = _snapshot(monkeypatch, tmp_path, FEED_MAYORIA)
    especiales = [v for v in snapshot["vehicles"] if v["trip_id"].startswith("SPECIAL_")]
    assert len(especiales) > len(snapshot["vehicles"]) - len(especiales)
    boards = _boards(monkeypatch, tmp_path, snapshot)
    assert boards["service"] == "1092X"
    assert boards["health"]["schedule_missing"] is False
    assert boards["stations_with_departures"] > 0


def test_especiales_sobre_el_umbral_no_cargan_un_segundo_dia(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """2 de 5 (40 % > SECOND_SERVICE_MIN_SHARE) no son un segundo dia de servicio."""
    _boards(monkeypatch, tmp_path)
    assert "SPECI" not in capsys.readouterr().out


# --- Parada que no esta en el horario (mutacion B) --------------------------------


def test_especial_en_parada_sin_horario_tiene_tablon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boards = _boards(monkeypatch, tmp_path, _snapshot(monkeypatch, tmp_path, FEED_MAYORIA))
    est = boards["stations"]["18002"]
    assert est["destinations"] == []
    (d,) = est["departures"]
    assert d["train"] == "91921"
    assert d["special"] is True


# --- Fantasmas: snapshot conservado o viejo ---------------------------------------


def test_snapshot_degradado_no_muestra_especiales(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repro de la revision: snapshot de 08:41 conservado, tablon a las 23:59."""
    snapshot = _snapshot(monkeypatch, tmp_path)
    snapshot["degraded"] = True
    snapshot["stale_since"] = "2026-10-21T21:59:00+00:00"
    boards = _boards(monkeypatch, tmp_path, snapshot,
                     ahora=VISTO.replace(hour=23, minute=59, second=0))
    assert all(not d["special"] for e in boards["stations"].values() for d in e["departures"])
    assert boards["specials_on_boards"] == 0
    assert boards["health"]["specials_hidden_stale"] is True


@pytest.mark.parametrize("edad_s", [cloud_tick.STALE_WARNING_SECONDS, 3600])
def test_snapshot_viejo_sin_marca_tampoco(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, edad_s: float
) -> None:
    snapshot = _snapshot(monkeypatch, tmp_path, FEED_MAYORIA)
    boards = _boards(monkeypatch, tmp_path, snapshot, ahora=VISTO + timedelta(seconds=edad_s))
    assert "91887" not in _trenes(boards, "17009")
    assert "18002" not in boards["stations"]


def test_hora_del_especial_es_la_de_la_observacion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Conservado pero vigente (2 min): se ve, con la hora de 08:41 y no la de ahora."""
    snapshot = _snapshot(monkeypatch, tmp_path)
    snapshot["stale_since"] = "2026-10-21T06:43:09+00:00"
    boards = _boards(monkeypatch, tmp_path, snapshot, ahora=VISTO + timedelta(minutes=2))
    d = _trenes(boards, "17009")["91887"]
    assert d["eta"] == "08:41"
    assert d["in"] == -2
    assert boards["health"]["specials_hidden_stale"] is False


def test_snapshot_sin_generated_at_no_muestra_especiales(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(monkeypatch, tmp_path)
    del snapshot["generated_at"]
    assert "91887" not in _trenes(_boards(monkeypatch, tmp_path, snapshot), "17009")


# --- Salud: un especial no tapa tablones vacios ------------------------------------


def test_salud_no_cuenta_especiales(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boards = _boards(monkeypatch, tmp_path, _snapshot(monkeypatch, tmp_path, FEED_MAYORIA))
    assert boards["specials_on_boards"] == 3
    # 18002 solo tiene el especial: no cuenta como estacion con salidas.
    con_programadas = {
        k for k, e in boards["stations"].items() if any(not d["special"] for d in e["departures"])
    }
    assert boards["stations_with_departures"] == len(con_programadas)
    assert "18002" not in con_programadas


def test_horario_ausente_sigue_alertando_con_especiales(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """RB-001 con especiales circulando: la salud dice lo mismo que sin ellos."""
    boards = _boards(monkeypatch, tmp_path, con_horario=False)
    assert boards["specials_on_boards"] > 0, "los especiales siguen visibles"
    assert boards["stations_with_departures"] == 0
    assert boards["health"]["schedule_missing"] is True
    assert boards["health"]["no_departures"] is True
    assert '"alert": "schedule_missing"' in capsys.readouterr().err


def test_solo_especiales_alerta_de_servicio_desconocido(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    snapshot = _snapshot(monkeypatch, tmp_path)
    snapshot["vehicles"] = [v for v in snapshot["vehicles"] if v["trip_id"].startswith("SPECIAL_")]
    boards = _boards(monkeypatch, tmp_path, snapshot)
    assert boards["service"] == ""
    assert boards["health"]["service_unknown_with_vehicles"] is True
    assert boards["stations_with_departures"] == 0
    assert '"alert": "service_unknown_with_vehicles"' in capsys.readouterr().err


def test_con_servicio_no_hay_alerta_de_servicio_desconocido(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert _boards(monkeypatch, tmp_path)["health"]["service_unknown_with_vehicles"] is False


# --- Final de linea -----------------------------------------------------------------


def test_especial_en_cabecera_se_marca_como_posible_llegada(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot = _snapshot(monkeypatch, tmp_path)
    llegando = copy.deepcopy(
        next(v for v in snapshot["vehicles"] if v["trip_id"] == "SPECIAL_10_91887C4B")
    )
    llegando.update(trip_id="SPECIAL_10_91899C4B", next_stop_id="17005")
    snapshot["vehicles"].append(llegando)
    boards = _boards(monkeypatch, tmp_path, snapshot)
    assert _trenes(boards, "17005")["91899"]["at_terminus"] is True
    assert _trenes(boards, "17009")["91887"]["at_terminus"] is False


def test_especial_mal_formado_no_va_al_tablon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    snapshot = _snapshot(monkeypatch, tmp_path)
    snapshot["vehicles"].append(
        {"trip_id": "SPECIAL_10_91887", "next_stop_id": "18099", "status": "STOPPED_AT"}
    )
    boards = _boards(monkeypatch, tmp_path, snapshot)
    assert "18099" not in boards["stations"]
    assert all(d["train"] != "9188" for e in boards["stations"].values() for d in e["departures"])
