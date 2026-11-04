"""Contrato: boards.json lleva la ventana de salidas y la próxima por destino (RB-017).

Caso real 04/11/2026 13:43, Madrid-Chamartín (17000): con el corte fijo de 10
salidas el 20460 (C4b → Parla, 13:47, en hora) no estaba en boards.json, y el
filtro «→ Parla» de la web no tenía nada que enseñar.
"""

from __future__ import annotations

import json
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

SERV = "1308X"
AHORA = datetime(2026, 11, 4, 13, 43, 5, tzinfo=cloud_tick.MADRID_TZ)
OTROS = ("Alcalá de Henares", "Madrid-Aeropuerto T4", "Aranjuez", "Villalba de Guadarrama")


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def _parada(num: str, hora: str, dest: str, ruta: str) -> dict[str, Any]:
    return {"trip": f"{SERV}{num}{ruta}", "t": hora, "seq": 5, "dest": dest,
            "route": ruta, "terminates": False}


def _escenario() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    paradas: list[dict[str, Any]] = []
    vehiculos: list[dict[str, Any]] = []

    def vivo(p: dict[str, Any], retraso_min: int, anden: str) -> None:
        vehiculos.append({"trip_id": p["trip"], "delay_seconds": retraso_min * 60,
                          "platform": anden, "next_stop_id": "17000", "status": "IN_TRANSIT_TO"})

    for i, (hora, retraso) in enumerate((("13:38", 6), ("13:39", 7), ("13:40", 7))):
        p = _parada(f"2100{i}", f"{hora}:00", OTROS[i], "C1")
        paradas.append(p)
        vivo(p, retraso, "5")
    for i in range(9):
        p = _parada(f"2200{i}", f"13:{43 + i % 4}:00", OTROS[i % 4], "C3")
        paradas.append(p)
        vivo(p, 0, "3")
    parla = _parada("20460", "13:47:00", "Parla", "C4b")
    paradas.append(parla)
    vivo(parla, 0, "6")
    for k, m in enumerate(range(13 * 60 + 48, 15 * 60 + 41, 2)):
        paradas.append(_parada(f"23{k:03d}", f"{m // 60:02d}:{m % 60:02d}:00", OTROS[k % 4], "C2"))
    paradas.append(_parada("20555", "14:55:00", "Colmenar Viejo", "C4b"))
    paradas.sort(key=lambda p: p["t"])
    return {"service": SERV, "stations": {"17000": paradas}}, vehiculos


def _boards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    horario, vehiculos = _escenario()
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True)
    (web / "horarios" / f"{SERV}.json").write_text(json.dumps(horario))
    snapshot = {"generated_at": AHORA.isoformat(), "vehicles": vehiculos}
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    return cloud_tick._build_boards(snapshot, LocalSnapshotPublisher(web))


def test_el_20460_a_parla_esta_en_boards_json_de_chamartin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boards = _boards(monkeypatch, tmp_path)
    salidas = boards["stations"]["17000"]["departures"]
    parla = [d for d in salidas if d["dest"] == "Parla"]
    assert [d["train"] for d in parla][:1] == ["20460"]
    assert parla[0]["eta"] == "13:47"


def test_boards_json_cubre_la_ventana_y_la_proxima_por_destino(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    boards = _boards(monkeypatch, tmp_path)
    salidas = boards["stations"]["17000"]["departures"]
    assert len(salidas) <= cloud_tick.BOARD_MAX_DEPARTURES
    assert len(salidas) > 10
    # Todo destino con salida en las próximas 2 h tiene al menos una en el tablón.
    assert {d["dest"] for d in salidas} >= {"Parla", "Colmenar Viejo", *OTROS}
    # Las que entran por ventana llegan hasta su borde (o hasta el tope duro).
    ventana = [d for d in salidas if d["in"] <= cloud_tick.BOARD_WINDOW_MINUTES]
    assert len(ventana) == len(salidas) - 1 or len(salidas) == cloud_tick.BOARD_MAX_DEPARTURES
