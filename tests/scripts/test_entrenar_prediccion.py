"""``scripts/entrenar_prediccion.py`` (RB-021): exportación de árboles y días excluidos.

Necesita lightgbm, pandas y pyarrow; sin ellos se salta (como en CI):
``uv run --with lightgbm --with pandas --with pyarrow pytest tests/scripts``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

lgb = pytest.importorskip("lightgbm")
pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("pyarrow")

from cercanias_domain import prediccion as P  # noqa: E402

RAIZ = Path(__file__).resolve().parents[2]


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "entrenar_prediccion", RAIZ / "scripts/entrenar_prediccion.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["entrenar_prediccion"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_los_dias_excluidos_son_los_seis_acordados() -> None:
    dias = _script().dias_excluidos()
    assert sorted(dias) == [
        "2026-10-29",
        "2026-11-10",
        "2026-11-11",
        "2026-11-12",
        "2026-11-16",
        "2026-11-17",
    ]


def test_un_dia_excluido_ni_se_lee(tmp_path: Path) -> None:
    s = _script()
    (tmp_path / "2026-11-17.parquet").write_bytes(b"no es parquet: si se leyera, fallaria")
    with pytest.raises(SystemExit, match="sin Parquet"):
        s.cargar(tmp_path, date(2026, 11, 17), date(2026, 11, 17), s.dias_excluidos())


def test_arboles_exportados_dan_lo_mismo_que_lightgbm() -> None:
    """Paridad con categóricas, ausentes y rasgos del modelo real (nombres y orden)."""
    s = _script()
    rasgos = P.RASGOS_POR_MODELO[P.MODELO_ENTRANTE_VIVO]
    rng = np.random.default_rng(0)
    n = 4000
    X = pd.DataFrame({k: rng.normal(size=n) for k in rasgos})
    for c in P.RASGOS_CATEGORICOS:
        X[c] = pd.Categorical(rng.choice(["C1", "C5", "18000", "35600", "70103"], size=n))
    X.loc[rng.random(n) < 0.3, "inc_live_delay"] = np.nan
    X.loc[rng.random(n) < 0.2, "h_med"] = 0.0
    y = 3 * X.inc_live_delay.fillna(-2) + (X.familia_c == "C5") * 2 + X.h_med + rng.normal(size=n)
    bst = lgb.train({**s.PARAMS, "min_data_in_leaf": 20, "num_threads": 1}, lgb.Dataset(X, y), 40)
    exportado = s.exportar_modelo(bst, X, rasgos)
    modelo = P.ModeloLgbm.from_json(json.loads(json.dumps(exportado)), rasgos)
    ref = bst.predict(X)
    for (_, fila), r in zip(X.head(500).iterrows(), ref[:500], strict=True):
        vals = {
            k: (None if pd.isna(v) else (str(v) if k in P.RASGOS_CATEGORICOS else float(v)))
            for k, v in fila.items()
        }
        assert modelo.predecir(vals) == pytest.approx(float(r), abs=1e-9)
