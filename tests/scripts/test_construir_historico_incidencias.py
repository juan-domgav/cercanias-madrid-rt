"""Columnas de incidencias de ``scripts/construir_historico.py`` (RB-010, RB-014).

Necesita pyarrow (grupo ``historico``); sin él se salta:
``uv run --group historico pytest tests/scripts``. Sin red ni GCS: ``_gcloud`` va
simulado.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from cercanias_domain.historico import ParadaProgramada
from cercanias_domain.historico_incidencias import foto_desde_objeto

pytest.importorskip("pyarrow")

RAIZ = Path(__file__).resolve().parents[2]
DIA = date(2026, 10, 21)
COLS = ["trip", "delay", "next_stop", "status"]


@pytest.fixture(scope="module")
def ch() -> ModuleType:
    ruta = RAIZ / "scripts/construir_historico.py"
    spec = importlib.util.spec_from_file_location("construir_historico", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["construir_historico"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def _pasada(tmp: Path, hhmmss: str, trenes: list[list[Any]], **extra: Any) -> Path:
    f = tmp / f"{hhmmss}.json"
    ts = f"{DIA.isoformat()}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}+02:00"
    f.write_text(json.dumps({"ts": ts, "cols": COLS, "trains": trenes, **extra}), encoding="utf-8")
    return f


def _horario(ch: ModuleType) -> Any:
    viajes = {
        "1092X20001C3": [ParadaProgramada(1, "10000", "08:00:00"), ParadaProgramada(2, "18000", "08:20:00"),
                         ParadaProgramada(3, "35000", "08:40:00")],
        "1092X20003C3": [ParadaProgramada(1, "10000", "08:30:00"), ParadaProgramada(2, "18000", "08:50:00")],
        "1092X20005C4b": [ParadaProgramada(1, "17000", "09:00:00"), ParadaProgramada(2, "18000", "09:15:00")],
    }
    return ch.Horario(
        fuente="prueba",
        viajes=viajes,
        ruta={t: t[-3:].rstrip("b") if t.endswith("b") else t[-2:] for t in viajes},
        destino={t: p[-1].stop_id for t, p in viajes.items()},
    )


def _tabla(
    ch: ModuleType, tmp: Path, *, con_registro: bool, fotos: list[Any] | None = None
) -> list[dict[str, Any]]:
    extra: dict[str, Any] = (
        {"canc": ["1092X20003C3"], "skip": {"1092X20001C3": ["35000"]}} if con_registro else {}
    )
    ficheros = [
        _pasada(tmp, "074500", [["1092X20001C3", 0, "10000", "STOPPED_AT"]], **extra),
        _pasada(tmp, "081000", [["1092X20001C3", 60, "18000", "IN_TRANSIT_TO"]], **extra),
    ]
    tabla, calidad = ch.construir(DIA, ficheros, [], _horario(ch), {}, fotos_avisos=fotos)
    filas: list[dict[str, Any]] = tabla.to_pylist()
    filas[0]["_calidad"] = calidad
    return filas


def _fila(filas: list[dict[str, Any]], trip: str, stop: str) -> dict[str, Any]:
    return next(f for f in filas if f["trip_id"] == trip and f["parada_id"] == stop)


def test_columnas_nuevas_en_el_esquema(ch: ModuleType) -> None:
    nombres = ch.COLUMNAS.names
    for c in ("cancelado", "parada_omitida", "recorrido_acortado", "alerta_activa_linea",
              "alerta_tipo", "min_desde_alerta", "n_cancelados_linea_hora"):
        assert c in nombres
    assert ch.VERSION == 3


def test_sin_registro_todo_nulo(ch: ModuleType, tmp_path: Path) -> None:
    filas = _tabla(ch, tmp_path, con_registro=False)
    for f in filas:
        for c in ("cancelado", "parada_omitida", "recorrido_acortado", "alerta_activa_linea",
                  "alerta_tipo", "min_desde_alerta", "n_cancelados_linea_hora"):
            assert f[c] is None, (c, f["trip_id"], f["parada_id"])
    calidad = filas[0]["_calidad"]
    assert calidad["pasadas_con_incidencias"] == 0
    assert calidad["trenes_cancelados"] is None
    assert calidad["fotos_avisos"] == 0


def test_con_registro_rellena_cancelaciones_y_omitidas(ch: ModuleType, tmp_path: Path) -> None:
    filas = _tabla(ch, tmp_path, con_registro=True)
    cancelado = _fila(filas, "1092X20003C3", "10000")
    assert cancelado["cancelado"] is True
    assert cancelado["n_cancelados_linea_hora"] == 1  # C3, salida 08:30

    omitido = _fila(filas, "1092X20001C3", "35000")
    assert omitido["cancelado"] is False
    assert omitido["parada_omitida"] is True
    assert omitido["recorrido_acortado"] is True  # omite su destino
    assert _fila(filas, "1092X20001C3", "18000")["parada_omitida"] is False

    otro = _fila(filas, "1092X20005C4b", "17000")
    assert (otro["cancelado"], otro["parada_omitida"], otro["recorrido_acortado"]) == (False, False, False)
    assert otro["n_cancelados_linea_hora"] == 0  # registrado, ninguno en C4 a las 9

    calidad = filas[0]["_calidad"]
    assert calidad["pasadas_con_incidencias"] == 2
    assert calidad["trenes_cancelados"] == 1


def test_avisos_por_linea(ch: ModuleType, tmp_path: Path) -> None:
    foto = foto_desde_objeto({
        "ts": "2026-10-21T06:00:00+02:00",
        "alerts": [{"id": "A", "lines": ["C3"], "start": "2026-10-21T07:30:00+02:00", "end": None,
                    "tipo": "averia"}],
    })
    filas = _tabla(ch, tmp_path, con_registro=False, fotos=[foto])
    c3 = _fila(filas, "1092X20001C3", "18000")  # programada 08:20
    assert (c3["alerta_activa_linea"], c3["alerta_tipo"], c3["min_desde_alerta"]) == (True, "averia", 50)
    c4 = _fila(filas, "1092X20005C4b", "17000")
    assert (c4["alerta_activa_linea"], c4["alerta_tipo"], c4["min_desde_alerta"]) == (False, None, None)


def _ok(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gcloud"], code, stdout, stderr)


def test_descargar_avisos_sin_objetos(
    ch: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ch, "_gcloud", lambda *a, **k: _ok(stderr="ERROR: matched no objects", code=1))
    assert ch.descargar_avisos(DIA, tmp_path) == []


def test_descargar_avisos_error_de_listado(ch: ModuleType, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ch, "_gcloud", lambda *a, **k: _ok(stderr="403 Forbidden", code=1))
    with pytest.raises(RuntimeError):
        ch.descargar_avisos(DIA, tmp_path)


def test_descargar_avisos_elige_la_vigente_y_las_del_dia(ch: ModuleType, tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    base = ch.ALERTAS
    objetos = [f"{base}/2026-10-07/101500.json", f"{base}/2026-10-19/120000.json",
               f"{base}/2026-10-21/000500.json", f"{base}/2026-10-21/181000.json",
               f"{base}/2026-10-22/031000.json", f"{base}/2026-10-22/090000.json"]
    copiados: list[str] = []

    def falso(*args: str, **_: Any) -> subprocess.CompletedProcess[str]:
        if args[0] == "ls":
            return _ok(stdout="\n".join(objetos))
        copiados.append(args[1])
        Path(args[2]).write_text(json.dumps({"ts": "2026-10-21T00:05:00+02:00", "alerts": []}))
        return _ok()

    monkeypatch.setattr(ch, "_gcloud", falso)
    ficheros = ch.descargar_avisos(DIA, tmp_path)
    assert copiados == objetos[1:5]
    assert len(ficheros) == 4
    assert len(ch.leer_avisos([*ficheros, tmp_path / "no-existe.json"])) == 4
