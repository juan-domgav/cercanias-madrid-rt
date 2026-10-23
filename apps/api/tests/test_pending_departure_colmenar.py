"""RB-009: el delay que Renfe publica ANTES de salir de cabecera no es una medida.

Pasadas reales del archivo del 22/10/2026 (solo C4) + horario de Colmenar Viejo y
Tres Cantos. Salidas reales (transicion STOPPED_AT -> IN_TRANSIT_TO en Colmenar):
20428 08:33 (+0), 20430 08:51:50 (+8), 20440 10:18:55 (+20). Renfe publico delay
0 para 20430 hasta que salio. Mientras, los que bajaban a Colmenar iban +11..+17.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, tzinfo
from pathlib import Path
from typing import Any

import pytest
from cercanias_api.bot import BotContext, handle_update

from cercanias_domain.station_view import Station
from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"
COLMENAR = Station("17005", "Colmenar Viejo", 40.6452, -3.7766)
TRES_CANTOS = Station("17004", "Tres Cantos", 40.6011, -3.7098)


def _reloj(momento: datetime) -> type[datetime]:
    class _Congelado(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> _Congelado:
            return cls.fromtimestamp(momento.timestamp(), tz=tz)

    return _Congelado


def _boards(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fixture: str,
            hora: tuple[int, int, int]) -> dict[str, Any]:
    web = tmp_path / "web"
    (web / "horarios").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "horarios_1093J_colmenar_rb008.json", web / "horarios" / "1093J.json")
    snap = json.loads((FIXTURES / fixture).read_text())
    ahora = datetime(2026, 10, 22, *hora, tzinfo=cloud_tick.MADRID_TZ)
    monkeypatch.setattr(cloud_tick, "datetime", _reloj(ahora))
    resultado: dict[str, Any] = cloud_tick._build_boards(snap, LocalSnapshotPublisher(web))
    return resultado


def _fila(boards: dict[str, Any], stop: str, tren: str) -> dict[str, Any]:
    return next(d for d in boards["stations"][stop]["departures"] if d["train"] == tren)


def _proximos(boards: dict[str, Any], station: Station) -> str:
    r = handle_update({"message": {"chat": {"id": 1}, "text": f"/proximos {station.name}"}},
                      BotContext(stations=[COLMENAR, TRES_CANTOS], boards=boards))
    assert r.reply is not None
    return r.reply.text


def test_0836_cero_de_cabecera_no_es_en_hora(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    b = _boards(monkeypatch, tmp_path, "snapshot_colmenar_20428_2026-10-22T0836.json", (8, 36, 12))
    d = _fila(b, "17005", "20430")
    assert (d["live"], d["pending_departure"], d["delay"], d["on_time"]) == (True, True, None, False)
    assert d["platform"] == "3", "el anden del feed sigue valiendo"
    assert d["realtime_status"] == "pending_departure"
    assert d["display"]["label"] == "pendiente de salir · Renfe aún no da retraso"
    # Ya salido: el dato en vivo manda (RB-008).
    d = _fila(b, "17005", "20428")
    assert (d["pending_departure"], d["delay"], d["display"]["label"]) == (False, 0, "en hora")
    # En Tres Cantos, el 20430 que aun no ha salido de Colmenar tampoco es "en hora".
    d = _fila(b, "17004", "20430")
    assert d["pending_departure"] is True and d["display"]["label"].startswith("pendiente de salir")
    assert b["health"]["departures_contradicting_live"] == 0
    assert b["health"]["departures_pending_departure"] >= 3


def test_0847_pasada_su_hora_y_aun_en_el_anden_dice_lo_que_lleva(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """20430 (08:43) sigue STOPPED_AT a las 08:47 con delay 0 del feed: +4 es una
    cota inferior cierta (salio +8). Y no desaparece del tablon a los 3 min."""
    b = _boards(monkeypatch, tmp_path, "snapshot_colmenar_2026-10-22T0847.json", (8, 47, 10))
    d = _fila(b, "17005", "20430")
    assert (d["delay"], d["eta"], d["pending_departure"]) == (4, "08:47", True)
    assert d["display"]["label"] == "+4 min · aún sin salir"
    t = _proximos(b, COLMENAR)
    esperado = ("<b>08:47</b> <s>08:43</s> → Parla\n"
                "   C4b tren 20430 · Andén <b>3</b> · +4 min · aún sin salir")
    assert esperado in t
    assert "en hora" not in t


def test_0847_los_que_suben_a_colmenar_conservan_su_retraso_en_vivo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Norte: 20429 y 20431 circulan +12: dato medido, se muestra tal cual, y el
    20429 (08:33 en Tres Cantos) sigue en el tablon a las 08:47 porque llega 08:45."""
    b = _boards(monkeypatch, tmp_path, "snapshot_colmenar_2026-10-22T0847.json", (8, 47, 10))
    for tren in ("20429", "20431"):
        d = _fila(b, "17004", tren)
        assert (d["live"], d["pending_departure"], d["delay"]) == (True, False, 12)
        assert d["display"]["label"] == "+12 min"
    t = _proximos(b, TRES_CANTOS)
    assert "C4b tren 20429 · Andén <b>2</b> · +12 min" in t


def test_1010_veinte_minutos_tarde_en_el_anden(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """20440 (09:58) aun STOPPED_AT a las 10:10 (salio 10:18, +20): +12 como minimo."""
    b = _boards(monkeypatch, tmp_path, "snapshot_colmenar_2026-10-22T1010.json", (10, 10, 11))
    d = _fila(b, "17005", "20440")
    assert d["display"]["label"] == "+12 min · aún sin salir"
    assert d["display"]["tone"] == "late"
    d = _fila(b, "17005", "20442")
    assert d["display"]["label"] == "pendiente de salir · Renfe aún no da retraso"
