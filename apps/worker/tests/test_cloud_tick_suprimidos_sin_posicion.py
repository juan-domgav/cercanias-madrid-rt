"""Contrato: un viaje SUPRIMIDO sin posición sale SUPRIMIDO en el tablón (RB-019).

Caso real, huelga del 17/11/2026 08:54:51 en Colmenar Viejo (17005): el
trip_updates de Renfe publicaba ``1020M20432C4b`` y ``1020M20436C4b`` como
``CANCELED`` pero, como no circulaban, no estaban en vehicle_positions. La
ingesta sólo marcaba ``cancelled`` en trenes YA presentes en
``snapshot["vehicles"]``: un viaje cancelado sin posición nunca llegaba a
``LiveTrain`` y el tablón decía «sin datos en vivo · aún no localizado» con el
aviso amarillo de «sin confirmar». Fixtures: archivo compacto de esa pasada y
horarios/1020M.json de Colmenar Viejo y Tres Cantos.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest

from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
ARCHIVO = FIXTURES / "archivo_huelga_2026-11-17T085451.json"
HORARIO = FIXTURES / "horarios_1020M_colmenar_huelga_2026-11-17.json"
AHORA = datetime(2026, 11, 17, 8, 54, 51, tzinfo=cloud_tick.MADRID_TZ)
COLMENAR = "17005"
TRES_CANTOS = "17004"


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:  # type: ignore[override]
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def pasada_de_huelga() -> tuple[dict[str, Any], frozenset[str], dict[str, frozenset[str]]]:
    """Snapshot, suprimidos y paradas saltadas tal como los vio la ingesta."""
    archivo = json.loads(ARCHIVO.read_text(encoding="utf-8"))
    cols = archivo["cols"]
    vehiculos = []
    for fila in archivo["trains"]:
        t = dict(zip(cols, fila, strict=True))
        vehiculos.append({
            "trip_id": t["trip"], "delay_seconds": t["delay"], "next_stop_id": t["next_stop"],
            "status": t["status"], "platform": t["platform"],
        })
    snapshot = {"generated_at": archivo["ts"], "vehicles": vehiculos}
    saltadas = {k: frozenset(v) for k, v in archivo["skip"].items()}
    return snapshot, frozenset(archivo["canc"]), saltadas


def construir_tablones(web: Path) -> dict[str, Any]:
    (web / "horarios").mkdir(parents=True, exist_ok=True)
    shutil.copy(HORARIO, web / "horarios" / "1020M.json")
    snapshot, suprimidos, saltadas = pasada_de_huelga()
    resultado: dict[str, Any] = cloud_tick._build_boards(
        snapshot, LocalSnapshotPublisher(web), suprimidos, saltadas
    )
    return resultado


@pytest.fixture
def boards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    return construir_tablones(tmp_path / "web")


def _fila(boards: dict[str, Any], estacion: str, tren: str) -> dict[str, Any]:
    return next(d for d in boards["stations"][estacion]["departures"] if d["train"] == tren)


def test_el_fixture_reproduce_el_caso() -> None:
    snapshot, suprimidos, _ = pasada_de_huelga()
    en_vivo = {v["trip_id"] for v in snapshot["vehicles"]}
    assert {"1020M20432C4b", "1020M20436C4b"} <= suprimidos
    assert not suprimidos & en_vivo, "los suprimidos de la huelga no tenían posición"


@pytest.mark.parametrize("tren", ["20432", "20436"])
def test_suprimido_sin_posicion_sale_suprimido(boards: dict[str, Any], tren: str) -> None:
    fila = _fila(boards, COLMENAR, tren)
    assert fila["cancelled"] is True
    assert fila["display"]["kind"] == "cancelled"
    assert fila["display"]["label"] == "SUPRIMIDO"
    # No es «sin confirmar»: la supresión es un dato publicado por Renfe.
    assert fila["unconfirmed"] is False
    assert all(n["kind"] != "unconfirmed" for n in fila["display"]["notices"])


def test_suprimido_sin_posicion_no_finge_estar_localizado(boards: dict[str, Any]) -> None:
    """Sin posición no hay andén ni retraso: ``live`` sigue siendo False."""
    fila = _fila(boards, COLMENAR, "20432")
    assert fila["live"] is False
    assert fila["platform"] is None
    assert fila["delay"] is None
    assert fila["realtime_status"] == "cancelled"


def test_suprimido_sin_posicion_en_parada_intermedia(boards: dict[str, Any]) -> None:
    """Tres Cantos (parada 2 del 20436): también SUPRIMIDO, no sólo en cabecera."""
    assert _fila(boards, TRES_CANTOS, "20436")["cancelled"] is True


def test_un_suprimido_no_cubre_el_sentido(boards: dict[str, Any]) -> None:
    """RB-017/RB-018: la próxima a Parla que SÍ sale (20440, 09:58, a 64 min:
    fuera de la ventana de 60) tiene que estar en el tablón. Antes el 20432 sin
    marcar «cubría» Parla y la web, filtrando «→ Parla», sólo enseñaba
    suprimidos con cara de «sin datos»."""
    parla = [d for d in boards["stations"][COLMENAR]["departures"] if d["dest"] == "Parla"]
    assert [d["train"] for d in parla] == ["20432", "20436", "20440"]
    assert [d["cancelled"] for d in parla] == [True, True, False]


def test_suprimidos_fuera_de_las_salidas_ausentes(boards: dict[str, Any]) -> None:
    """Un suprimido no es una salida «sin rastro» (vigía) y sí se cuenta aparte."""
    filas = [d for e in boards["stations"].values() for d in e["departures"]]
    assert not any(d["cancelled"] and d["realtime_status"] == "no_data" for d in filas)
    salud = boards["health"]
    assert salud["departures_cancelled"] == sum(1 for d in filas if d["cancelled"])
    assert salud["departures_cancelled"] >= 4
    assert salud["cancelled_trips_without_position"] == 50


def test_un_viaje_sin_suprimir_ni_posicion_sigue_sin_datos(boards: dict[str, Any]) -> None:
    """El arreglo no marca de más: el 20440 (no suprimido, aún no publicado)."""
    fila = _fila(boards, COLMENAR, "20440")
    assert fila["cancelled"] is False
    assert fila["live"] is False


def test_un_especial_suprimido_sin_posicion_no_entra_en_el_tablon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Un especial no tiene horario con el que casar: sin posición no hay
    estación donde pintarlo, y no debe activar ``specials_hidden_stale``."""
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(AHORA))
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True)
    shutil.copy(HORARIO, web / "horarios" / "1020M.json")
    snapshot, suprimidos, saltadas = pasada_de_huelga()
    boards = cloud_tick._build_boards(
        {**snapshot, "degraded": True}, LocalSnapshotPublisher(web),
        suprimidos | {"SPECIAL_10_91887C4B"}, saltadas,
    )
    assert boards["specials_on_boards"] == 0
    assert boards["health"]["specials_hidden_stale"] is False
