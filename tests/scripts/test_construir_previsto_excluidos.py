"""``scripts/construir_previsto.py`` salta los días excluidos (RB-013).

Necesita pyarrow (grupo ``historico``); sin él se salta:
``uv run --group historico pytest tests/scripts``. Sin red ni GCS: el histórico
es un directorio local de Parquet de prueba, que el script sólo lee.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("pyarrow")
import pyarrow as pa  # type: ignore[import-untyped]
import pyarrow.parquet as pq  # type: ignore[import-untyped]

RAIZ = Path(__file__).resolve().parents[2]
NORMALES = [date(2026, 10, d) for d in (20, 21, 22, 23, 26)]
HUELGA = date(2026, 10, 29)


@pytest.fixture(scope="module")
def cp() -> ModuleType:
    ruta = RAIZ / "scripts/construir_previsto.py"
    spec = importlib.util.spec_from_file_location("construir_previsto", ruta)
    assert spec is not None and spec.loader is not None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules["construir_previsto"] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def _dia(dir_: Path, dia: date, retraso_min: float) -> Path:
    fila = {
        "fecha_servicio": dia.isoformat(), "tren": "19799", "familia": "C1", "parada_id": "18000",
        "hora": 8, "medido": True, "es_destino": False, "programada": "08:00", "es_especial": False,
        "retraso_s": retraso_min * 60,
    }
    ruta = dir_ / f"{dia.isoformat()}.parquet"
    pq.write_table(pa.Table.from_pylist([fila]), ruta)
    return ruta


@pytest.fixture
def historico(tmp_path: Path) -> Path:
    base = tmp_path / "historico"
    base.mkdir()
    for d in NORMALES:
        _dia(base, d, 2)
    _dia(base, HUELGA, 45)
    return base


def _construir(cp: ModuleType, historico: Path, tmp_path: Path, *extra: str) -> dict:
    out = tmp_path / "previsto.json"
    assert cp.main(["--origen", str(historico), "--desde", "2026-10-19", "--hasta", "2026-10-30",
                    "--out", str(out), *extra]) == 0
    return json.loads(out.read_text())


def test_el_dia_de_huelga_no_entra_en_la_mediana_y_se_informa(
    cp: ModuleType, historico: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    antes = {p: p.stat().st_mtime_ns for p in historico.iterdir()}
    tabla = _construir(cp, historico, tmp_path)
    assert tabla["entries"]["C1"]["18000"]["19799"]["laborable"] == [2.0, 5]
    assert tabla["history"]["days"] == len(NORMALES)
    assert "2026-10-29" in tabla["excluded_days"]
    informe = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert informe["excluded_days_skipped"] == ["2026-10-29"]
    assert informe["excluded_days_skipped_count"] == 1
    # El histórico NO se toca.
    assert {p: p.stat().st_mtime_ns for p in historico.iterdir()} == antes


def test_con_lista_vacia_es_como_antes(cp: ModuleType, historico: Path, tmp_path: Path) -> None:
    vacia = tmp_path / "vacia.json"
    vacia.write_text('{"excluded_days": []}', encoding="utf-8")
    tabla = _construir(cp, historico, tmp_path, "--dias-excluidos", str(vacia))
    assert tabla["entries"]["C1"]["18000"]["19799"]["laborable"] == [2.0, 6]
    assert tabla["excluded_days"] == []


def test_lista_malformada_para_sin_escribir(
    cp: ModuleType, historico: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    mala = tmp_path / "mala.json"
    mala.write_text('{"excluded_days": [{"date": "2026-10-29"}]}', encoding="utf-8")
    out = tmp_path / "previsto.json"
    assert cp.main(["--origen", str(historico), "--desde", "2026-10-19", "--hasta", "2026-10-30",
                    "--out", str(out), "--dias-excluidos", str(mala)]) == 2
    assert not out.exists()
    assert "falta 'reason'" in capsys.readouterr().err


def test_los_ficheros_de_dias_excluidos_ni_se_piden(cp: ModuleType, historico: Path) -> None:
    fechas = cp.dias(date(2026, 10, 28), date(2026, 10, 30))
    assert cp.dias_a_leer(fechas, frozenset({HUELGA})) == [date(2026, 10, 28), date(2026, 10, 30)]
