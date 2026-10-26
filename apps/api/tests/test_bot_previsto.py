"""RB-013 + RB-008: el bot y el tablón dicen LO MISMO de un tren previsto."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta, tzinfo
from pathlib import Path
from typing import Any

import pytest
from cercanias_api.bot import BotContext, format_departure, handle_update

from cercanias_domain.board_status import departure_status
from cercanias_domain.prevision import Observation, build_table
from cercanias_domain.station_view import Station
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

SERV = "1019L"
ATOCHA = Station("18000", "Madrid-Atocha Cercanías", 40.4065, -3.6893)
AHORA = datetime(2026, 10, 19, 8, 0, 30, tzinfo=cloud_tick.MADRID_TZ)


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def _boards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rutas: frozenset[str]) -> dict[str, Any]:
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True)
    paradas = [
        {"trip": f"{SERV}35003C5", "t": "08:10:00", "seq": 5, "dest": "Humanes", "route": "C5"},
        {"trip": f"{SERV}19799C1", "t": "08:20:00", "seq": 5, "dest": "Aeropuerto T4", "route": "C1"},
        {"trip": f"{SERV}35001C5", "t": "08:25:00", "seq": 5, "dest": "Móstoles-El Soto", "route": "C5"},
    ]
    (web / "horarios" / f"{SERV}.json").write_text(json.dumps({"stations": {ATOCHA.stop_id: paradas}}))
    inicio = date(2026, 9, 14)
    dias = [d for d in (inicio + timedelta(days=i) for i in range(14)) if d.weekday() < 5]
    obs = [Observation("C1", "19799", ATOCHA.stop_id, d, 180) for d in dias]
    obs += [Observation("C5", "35001", ATOCHA.stop_id, d, 20) for d in dias]
    obs += [Observation("C5", "35003", ATOCHA.stop_id, d, 600) for d in dias]
    privado = tmp_path / "privado"
    (privado / "prevision").mkdir(parents=True)
    (privado / cloud_tick.PREVISTO_KEY).write_text(json.dumps(build_table(
        obs, lines=frozenset({"C1", "C5"}), holidays=frozenset(), generated_at=AHORA.astimezone(UTC))))
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    monkeypatch.setattr(cloud_tick, "PREVISTO_ROUTES", rutas)
    snap = {"generated_at": (AHORA - timedelta(seconds=20)).isoformat(),
            "vehicles": [{"trip_id": f"{SERV}35003C5", "delay_seconds": 0, "next_stop_id": "18000",
                          "status": "IN_TRANSIT_TO", "platform": "6"}]}
    prevision = cloud_tick._load_prevision(LocalSnapshotPublisher(privado), AHORA)
    resultado: dict[str, Any] = cloud_tick._build_boards(
        snap, LocalSnapshotPublisher(web), prevision=prevision)
    return resultado


def _proximos(boards: dict[str, Any]) -> str:
    r = handle_update({"message": {"chat": {"id": 1}, "text": "/proximos Atocha"}},
                      BotContext(stations=[ATOCHA], boards=boards))
    assert r.reply is not None
    return r.reply.text


def test_bot_y_tablon_dicen_lo_mismo_del_tren_previsto(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    boards = _boards(tmp_path, monkeypatch, frozenset({"C1", "C5"}))
    filas = boards["stations"][ATOCHA.stop_id]["departures"]
    texto = _proximos(boards)
    for fila in filas:
        assert fila["display"] == departure_status(fila).to_json()
        assert fila["display"]["label"] in texto

    c1 = next(f for f in filas if f["train"] == "19799")
    assert format_departure(c1) == (
        "<b>08:20</b> → Aeropuerto T4\n"
        "   C1 tren 19799 · Andén — · previsto +3 min\n"
        "   📊 previsto por su histórico; Renfe aún no lo publica en vivo"
    )
    c5 = next(f for f in filas if f["train"] == "35001")
    assert "previsto puntual" in format_departure(c5)
    vivo = next(f for f in filas if f["train"] == "35003")
    assert format_departure(vivo).splitlines()[1].endswith("Andén <b>6</b> · en hora")
    assert "«previsto» = previsión por el histórico de ese tren, no dato en vivo" in texto


def test_sin_prevision_el_bot_no_cambia(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    texto = _proximos(_boards(tmp_path, monkeypatch, frozenset()))
    assert "previsto" not in texto
    assert texto.endswith("<i>Andén y retraso vienen del feed en vivo; si el tren aún no "
                          "está localizado, no se muestran.</i>")
