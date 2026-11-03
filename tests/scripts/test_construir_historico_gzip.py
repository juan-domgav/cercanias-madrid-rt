"""``scripts/construir_historico.py`` con objetos comprimidos con gzip (RB-016).

Desde el PR #20 ``horarios/*.json`` (y snapshot/boards) se guardan con
``Content-Encoding: gzip`` y ``gcloud storage cat`` los entrega comprimidos: el
build del 2026-11-02 cayó con ``'utf-8' codec can't decode byte 0x8b``. El script
debe leer igual JSON en claro, comprimido y una mezcla de ambos en el mismo día.

Necesita pyarrow (grupo ``historico``); sin él se salta:
``uv run --group historico pytest tests/scripts``. Sin red ni GCS.
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import stat
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytest.importorskip("pyarrow")

RAIZ = Path(__file__).resolve().parents[2]
DIA = date(2026, 11, 2)
COLS = ["trip", "delay", "next_stop", "status"]
TRIP = "1005L20001C3"
HORARIO = {
    "service": "1005L",
    "stations": {
        "10000": [{"trip": TRIP, "seq": 1, "t": "08:00:00", "route": "C3", "dest": "35000"}],
        "18000": [{"trip": TRIP, "seq": 2, "t": "08:20:00", "route": "C3", "dest": "35000"}],
        "35000": [{"trip": TRIP, "seq": 3, "t": "08:40:00", "route": "C3", "dest": "35000"}],
    },
}
#: Pasadas de un tren que sale de 10000, pasa 18000 y llega a 35000 (4 ficheros).
PASADAS: list[tuple[str, list[Any]]] = [
    ("075900", [TRIP, 0, "10000", "STOPPED_AT"]),
    ("080130", [TRIP, 90, "18000", "IN_TRANSIT_TO"]),
    ("082230", [TRIP, 150, "35000", "IN_TRANSIT_TO"]),
    ("084200", [TRIP, 120, "35000", "STOPPED_AT"]),
]


@pytest.fixture(scope="module")
def ch() -> ModuleType:
    ruta = RAIZ / "scripts/construir_historico.py"
    spec = importlib.util.spec_from_file_location("construir_historico_gzip", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["construir_historico_gzip"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def _bytes_pasada(hhmmss: str, tren: list[Any]) -> bytes:
    ts = f"{DIA.isoformat()}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}+01:00"
    # Como lo escribe la ingesta: NDJSON de una línea con salto final y UTF-8.
    return (
        json.dumps(
            {"ts": ts, "n": 1, "cols": COLS, "trains": [tren], "canc": [], "skip": {}},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _dia(tmp: Path, comprimidos: set[int]) -> list[Path]:
    """Los ficheros del día; los de índice en ``comprimidos`` van con gzip."""
    tmp.mkdir(parents=True, exist_ok=True)
    ficheros = []
    for k, (hhmmss, tren) in enumerate(PASADAS):
        datos = _bytes_pasada(hhmmss, tren)
        f = tmp / f"{hhmmss}.json"
        f.write_bytes(gzip.compress(datos, mtime=0) if k in comprimidos else datos)
        ficheros.append(f)
    return ficheros


def _horario(ch: ModuleType) -> Any:
    return ch._horario_desde_json(HORARIO, "horarios/1005L.json")


def _construir(ch: ModuleType, ficheros: list[Path]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tabla, calidad = ch.construir(DIA, ficheros, [], _horario(ch), {})
    filas: list[dict[str, Any]] = tabla.to_pylist()
    return filas, calidad


def _sin_marca_de_tiempo(calidad: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in calidad.items() if k != "generado"}


# --- helper ---------------------------------------------------------------------


def test_json_guardado_en_claro(ch: ModuleType) -> None:
    assert ch.json_guardado(b'{"a": "\xc3\xb1"}') == {"a": "ñ"}


def test_json_guardado_comprimido(ch: ModuleType) -> None:
    assert ch.json_guardado(gzip.compress('{"a": "ñ"}'.encode())) == {"a": "ñ"}


def test_json_guardado_no_descomprime_dos_veces(ch: ModuleType) -> None:
    # Ya descomprimido por la transcodificación de GCS: un JSON nunca empieza por 1f 8b.
    datos = json.dumps({"x": list(range(5))}).encode()
    assert ch.json_guardado(datos) == {"x": [0, 1, 2, 3, 4]}


@pytest.mark.parametrize(
    "roto",
    [b"\x1f\x8b\x08\x00corrupto", gzip.compress(b'{"a": 1}')[:-6], b"\xff\xfe{", b"{no json"],
    ids=["gzip-corrupto", "gzip-truncado", "no-utf8", "json-invalido"],
)
def test_errores_de_lectura_son_errores_json(ch: ModuleType, roto: bytes) -> None:
    with pytest.raises(ch.ERRORES_JSON):
        ch.json_guardado(roto)


# --- archivo/ ---------------------------------------------------------------------


def test_dia_en_claro(ch: ModuleType, tmp_path: Path) -> None:
    filas, calidad = _construir(ch, _dia(tmp_path, set()))
    assert calidad["pasadas"] == len(PASADAS)
    assert calidad["trenes_programados_vistos"] == 1
    assert {f["parada_id"]: f["estado_medida"] for f in filas} == {
        "10000": "marcador",
        "18000": "medido",
        "35000": "medido",
    }  # en cabecera sólo se le vio parado: marcador (RB-009)


def test_dia_comprimido_igual_que_en_claro(ch: ModuleType, tmp_path: Path) -> None:
    claro, cal_claro = _construir(ch, _dia(tmp_path / "claro", set()))
    gz, cal_gz = _construir(ch, _dia(tmp_path / "gz", set(range(len(PASADAS)))))
    assert gz == claro
    assert _sin_marca_de_tiempo(cal_gz) == _sin_marca_de_tiempo(cal_claro)


def test_dia_mixto_igual_que_en_claro(ch: ModuleType, tmp_path: Path) -> None:
    """Como el 2026-11-01/02: parte del día en claro y parte comprimido."""
    claro, cal_claro = _construir(ch, _dia(tmp_path / "claro", set()))
    mixto, cal_mixto = _construir(ch, _dia(tmp_path / "mixto", {1, 3}))
    assert mixto == claro
    assert _sin_marca_de_tiempo(cal_mixto) == _sin_marca_de_tiempo(cal_claro)
    assert cal_mixto["pasadas"] == len(PASADAS)


def test_fichero_roto_se_salta_sin_tumbar_el_dia(ch: ModuleType, tmp_path: Path) -> None:
    ficheros = _dia(tmp_path, {0})
    roto = tmp_path / "090000.json"
    roto.write_bytes(gzip.compress(b'{"ts": "2026-11-02T09:00:00+01:00"')[:-4])
    lectura = ch.leer_observaciones([*ficheros, roto], DIA)
    assert len(lectura.marcas) == len(PASADAS)


def test_avisos_comprimidos_y_en_claro(ch: ModuleType, tmp_path: Path) -> None:
    def foto(hora: str) -> bytes:
        return json.dumps({"ts": f"2026-11-02T{hora}+01:00", "alerts": [{"id": hora}]}).encode()

    a = tmp_path / "a.json"
    a.write_bytes(foto("07:00:00"))
    b = tmp_path / "b.json"
    b.write_bytes(gzip.compress(foto("08:00:00")))
    fotos = ch.leer_avisos([b, a])
    assert [f.ts.hour for f in fotos] == [7, 8]


# --- horarios/ --------------------------------------------------------------------


@pytest.mark.parametrize("comprimido", [False, True], ids=["en-claro", "gzip"])
def test_cargar_horario(
    ch: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, comprimido: bool
) -> None:
    datos = json.dumps(HORARIO).encode()
    salida = gzip.compress(datos) if comprimido else datos
    llamadas: list[tuple[str, ...]] = []

    def falso(*args: str, **_: Any) -> subprocess.CompletedProcess[bytes]:
        llamadas.append(args)
        return subprocess.CompletedProcess(["gcloud"], 0, salida, b"")

    monkeypatch.setattr(ch, "_gcloud_binario", falso)
    horario = ch.cargar_horario(DIA, "1005L", tmp_path)
    assert llamadas == [("cat", f"{ch.HORARIOS}/1005L.json")]
    assert horario.fuente == "horarios/1005L.json"
    assert [p.stop_id for p in horario.viajes[TRIP]] == ["10000", "18000", "35000"]


@pytest.fixture
def gcloud_falso(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Un ``gcloud`` en el PATH que, como el real, saca el objeto crudo (gzip) por ``cat``."""
    binario = tmp_path / "bin"
    binario.mkdir()
    objeto = tmp_path / "objeto.json.gz"
    objeto.write_bytes(gzip.compress(json.dumps(HORARIO).encode(), mtime=0))
    script = binario / "gcloud"
    script.write_text(f'#!/bin/sh\n[ "$2" = cat ] && exec cat "{objeto}"\necho "uso: $*" >&2\nexit 2\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{binario}{os.pathsep}{os.environ['PATH']}")
    return objeto


@pytest.mark.skipif(sys.platform == "win32", reason="gcloud falso en sh")
@pytest.mark.usefixtures("gcloud_falso")
def test_regresion_cat_de_horario_comprimido(ch: ModuleType, tmp_path: Path) -> None:
    """Reproduce el fallo del 2026-11-02 por el camino real de subprocess."""
    r = ch._gcloud_binario("cat", f"{ch.HORARIOS}/1005L.json")
    assert r.stdout[:2] == b"\x1f\x8b"
    horario = ch.cargar_horario(DIA, "1005L", tmp_path)
    assert horario is not None and TRIP in horario.viajes


@pytest.mark.skipif(sys.platform == "win32", reason="gcloud falso en sh")
@pytest.mark.usefixtures("gcloud_falso")
def test_gcloud_texto_sigue_devolviendo_str(ch: ModuleType) -> None:
    r = ch._gcloud("ls", "gs://x/", entrada="a\nb")
    assert r.returncode == 2
    assert isinstance(r.stdout, str) and isinstance(r.stderr, str)
    assert "uso: storage ls gs://x/" in r.stderr
