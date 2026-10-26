#!/usr/bin/env python3
"""Verifica la previsión «previsto» (RB-013) contra la semana de prueba.

Dos medidas, ambas con entrenamiento 2026-09-08..10-18 y prueba 10-19..25 por
defecto:

1. **Reproducción del backtest** (``cercanias-watch/backtest.py``): mediana de
   (tren, parada, tipo de día) sin mínimo de muestras ni redondeo, si falta la
   de (línea, parada, hora, tipo de día) y si no 0. Debe dar C1 1.65 / 79.8 % y
   C5 3.14 / 62.1 % (MAE en min / % a ±2 min).
2. **Lo que se envía** (la tabla de ``construir_previsto.py``: mínimo 5
   muestras, sin el respaldo por hora, redondeo al minuto) medido con
   ``cercanias_domain.prevision.evaluate``, más un ida y vuelta por JSON con
   la misma validación que usa el worker.

Uso::

    uv run --with pyarrow python scripts/verificar_previsto.py --origen /ruta/historico \\
        [--json docs/informes/previsto-holdout.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from construir_previsto import Fila, festivos, leer_filas

from cercanias_domain.prevision import (
    DAY_TYPE_FESTIVO,
    OUTLIER_MAX_MINUTES,
    OUTLIER_MIN_MINUTES,
    WITHIN_MINUTES,
    PredictionTable,
    build_table,
    day_type,
    evaluate,
)

ESPERADO = {"C1": (1.65, 79.8), "C5": (3.14, 62.1)}


def _validas(filas: list[Fila]) -> list[Fila]:
    return [f for f in filas if OUTLIER_MIN_MINUTES <= f.retraso_s / 60 <= OUTLIER_MAX_MINUTES]


def reproducir_backtest(
    entreno: list[Fila], prueba: list[Fila], fest: frozenset[date], lineas: frozenset[str],
) -> dict[str, dict[str, Any]]:
    def fin(f: Fila) -> bool:
        return day_type(f.fecha, fest) == DAY_TYPE_FESTIVO

    por_tren: dict[tuple[str, str, bool], list[float]] = defaultdict(list)
    por_hora: dict[tuple[str, str, int, bool], list[float]] = defaultdict(list)
    for f in entreno:
        por_tren[(f.tren, f.parada, fin(f))].append(f.retraso_s / 60)
        por_hora[(f.familia, f.parada, f.hora, fin(f))].append(f.retraso_s / 60)
    mb = {k: statistics.median(v) for k, v in por_tren.items()}
    mc = {k: statistics.median(v) for k, v in por_hora.items()}

    errores: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for f in prueba:
        if f.familia not in lineas:
            continue
        y = f.retraso_s / 60
        p = mb.get((f.tren, f.parada, fin(f)), mc.get((f.familia, f.parada, f.hora, fin(f)), 0.0))
        errores[f.familia].append((abs(y - p), abs(y)))
    salida: dict[str, dict[str, Any]] = {}
    for linea, e in sorted(errores.items()):
        mae_p = statistics.fmean(a for a, _ in e)
        mae_0 = statistics.fmean(b for _, b in e)
        salida[linea] = {
            "n": len(e),
            "mae_predicted": round(mae_p, 2),
            "within2_predicted_pct": round(100 * sum(a <= WITHIN_MINUTES for a, _ in e) / len(e), 1),
            "mae_on_time": round(mae_0, 2),
            "within2_on_time_pct": round(100 * sum(b <= WITHIN_MINUTES for _, b in e) / len(e), 1),
            "gain_pct": round(100 * (mae_0 - mae_p) / mae_0, 1),
        }
    return salida


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origen", required=True, help="directorio con <día>.parquet, o 'gs' (solo lectura)")
    ap.add_argument("--entreno", default="2026-09-08..2026-10-18")
    ap.add_argument("--prueba", default="2026-10-19..2026-10-25")
    ap.add_argument("--lineas", default="C1,C5")
    ap.add_argument("--json", type=Path, help="escribe aquí el informe (solo métricas agregadas)")
    args = ap.parse_args(argv)

    e0, e1 = (date.fromisoformat(x) for x in args.entreno.split(".."))
    p0, p1 = (date.fromisoformat(x) for x in args.prueba.split(".."))
    lineas = frozenset(args.lineas.split(","))
    fest = festivos("")
    entreno = _validas(list(leer_filas(args.origen, e0, e1)))
    prueba = _validas(list(leer_filas(args.origen, p0, p1)))

    backtest = reproducir_backtest(entreno, prueba, fest, lineas)
    raw = json.dumps(build_table([f.observacion() for f in entreno], lines=lineas, holidays=fest,
                                 generated_at=datetime.now(tz=UTC))).encode()
    tabla = PredictionTable.from_bytes(raw)
    enviado = evaluate(tabla, [f.observacion() for f in prueba if f.familia in lineas], fest)

    ok = all(
        (backtest[ln]["mae_predicted"], backtest[ln]["within2_predicted_pct"]) == ESPERADO[ln]
        for ln in ESPERADO if ln in lineas
    )
    informe = {
        "entreno": args.entreno, "prueba": args.prueba, "history": tabla.history,
        "entries": tabla.entry_count, "artifact_bytes": len(raw),
        "backtest_reproducido": backtest, "esperado": {k: list(v) for k, v in ESPERADO.items()},
        "reproduce_backtest": ok, "tabla_enviada": enviado,
    }
    texto = json.dumps(informe, ensure_ascii=False, indent=1)
    print(texto)
    if args.json:
        args.json.write_text(texto + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
