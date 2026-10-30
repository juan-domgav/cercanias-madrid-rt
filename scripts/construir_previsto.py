#!/usr/bin/env python3
"""Construye el artefacto de previsión «previsto» (RB-013) desde el histórico.

Lee los Parquet diarios del histórico (RB-010, PR #10): de un directorio local o
de ``gs://cercanias-madrid-rt-privado/historico/`` (SOLO lectura, con
``gcloud storage cp``) y escribe en LOCAL la tabla JSON de medianas por
(línea, estación, tren, tipo de día). NO sube nada: subirla al bucket privado
es un paso manual del runbook RB-013.

Los días anómalos de ``packages/domain/src/cercanias_domain/previsto_dias_excluidos.json``
(huelgas, horario reducido) se saltan por completo: ni se descargan ni entran en
la mediana. Se informa de cuántos y cuáles. El histórico nunca se modifica.

El repo es público: el artefacto deriva de datos privados y NO se commitea.

Uso::

    uv run --with pyarrow python scripts/construir_previsto.py \\
        --origen /ruta/historico --desde 2026-09-08 --hasta 2026-10-25 --out /tmp/previsto.json
    uv run --with pyarrow python scripts/construir_previsto.py --origen gs \\
        --desde 2026-09-08 --hasta 2026-10-25 --out /tmp/previsto.json
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq  # type: ignore[import-untyped]

from cercanias_domain.prevision import (
    DEFAULT_EXCLUDED_DAYS_PATH,
    MIN_OBSERVATIONS,
    ExcludedDaysError,
    Observation,
    PredictionTable,
    build_table,
    load_excluded_days,
)

HISTORICO_GS = "gs://cercanias-madrid-rt-privado/historico"
# Festivos laborales de la Comunidad de Madrid (nacionales + autonómicos) desde
# que hay histórico (sept. 2026). Los locales de Madrid capital (09/11 Almudena,
# 15/05 San Isidro) NO están: no aplican a toda la red. Se añaden con --festivos
# o, en producción, con PREVISTO_FESTIVOS.
#
# 2026: calendario laboral de la Comunidad de Madrid para 2026 (desde el 12/10).
# 2027: Decreto 82/2026, de 30 de septiembre, del Consejo de Gobierno (BOCM
#   núm. 234, 1/10/2026, https://www.bocm.es/boletin/CM_Orden_BOCM/2026/10/01/BOCM-20261001-19.PDF),
#   que coincide con la columna «Madrid» de la Resolución de 8 de octubre de 2026
#   de la Dirección General de Trabajo (BOE núm. 254, 13/10/2026, BOE-A-2026-21254,
#   https://www.boe.es/diario_boe/txt.php?id=BOE-A-2026-21254). El 2 de mayo
#   (Fiesta de la Comunidad) cae en domingo y no se traslada; el 15 de agosto
#   (domingo) pasa al lunes 16. Sábados incluidos por completitud (ya son festivo).
FESTIVOS_CAM_2026 = ("2026-10-12", "2026-11-02", "2026-12-07", "2026-12-08", "2026-12-25")
FESTIVOS_CAM_2027 = (
    "2027-01-01",  # Año Nuevo
    "2027-01-06",  # Epifanía del Señor
    "2027-03-19",  # San José
    "2027-03-25",  # Jueves Santo
    "2027-03-26",  # Viernes Santo
    "2027-05-01",  # Fiesta del Trabajo (sábado)
    "2027-08-16",  # traslado de la Asunción de la Virgen
    "2027-10-12",  # Fiesta Nacional de España
    "2027-11-01",  # Todos los Santos
    "2027-12-06",  # Día de la Constitución Española
    "2027-12-08",  # Inmaculada Concepción
    "2027-12-25",  # Natividad del Señor (sábado)
)
FESTIVOS_CAM = (*FESTIVOS_CAM_2026, *FESTIVOS_CAM_2027)
COLUMNAS = ["fecha_servicio", "tren", "familia", "parada_id", "hora", "medido", "es_destino",
            "programada", "es_especial", "retraso_s"]


@dataclass(frozen=True)
class Fila:
    """Una salida medida del histórico, ya filtrada como en el backtest."""

    fecha: date
    tren: str
    familia: str
    parada: str
    hora: int
    retraso_s: float

    def observacion(self) -> Observation:
        return Observation(self.familia, self.tren, self.parada, self.fecha, self.retraso_s)


def dias(desde: date, hasta: date) -> list[date]:
    return [desde + timedelta(days=i) for i in range((hasta - desde).days + 1)]


def dias_a_leer(fechas: list[date], excluidos: frozenset[date]) -> list[date]:
    """Los días excluidos ni se piden al histórico."""
    return [d for d in fechas if d not in excluidos]


def _ficheros(origen: str, fechas: list[date], tmp: Path) -> list[Path]:
    if origen != "gs":
        base = Path(origen)
        locales = (base / f"{d.isoformat()}.parquet" for d in fechas)
        return [f for f in locales if f.exists()]
    rutas = [f"{HISTORICO_GS}/{d.isoformat()}.parquet" for d in fechas]
    # Solo lectura: copia a un temporal local. Los días que falten no paran el proceso.
    subprocess.run(["gcloud", "storage", "cp", "--continue-on-error", *rutas, str(tmp)],
                   check=False, capture_output=True)
    return sorted(tmp.glob("*.parquet"))


def leer_filas(origen: str, desde: date, hasta: date,
               excluidos: frozenset[date] = frozenset()) -> Iterator[Fila]:
    """Filtro del backtest: medido, no destino, con hora programada, no especial y con retraso.

    Los días de ``excluidos`` no se leen (y ``build_table`` vuelve a filtrarlos
    por ``fecha_servicio``, por si una fila cae en otro fichero).
    """
    with tempfile.TemporaryDirectory() as tmp:
        for fichero in _ficheros(origen, dias_a_leer(dias(desde, hasta), excluidos), Path(tmp)):
            for r in pq.read_table(fichero, columns=COLUMNAS).to_pylist():
                retraso = r["retraso_s"]
                if (not r["medido"] or r["es_destino"] or r["programada"] is None or r["es_especial"]
                        or retraso is None or not math.isfinite(retraso)):
                    continue
                fecha = r["fecha_servicio"]
                if isinstance(fecha, str):
                    fecha = date.fromisoformat(fecha)
                yield Fila(fecha, str(r["tren"]), str(r["familia"]), str(r["parada_id"]),
                           int(r["hora"]), float(retraso))


def festivos(extra: str) -> frozenset[date]:
    todos = (*FESTIVOS_CAM, *extra.split(","))
    return frozenset(date.fromisoformat(x.strip()) for x in todos if x.strip())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--origen", required=True, help="directorio con <día>.parquet, o 'gs' (solo lectura)")
    ap.add_argument("--desde", type=date.fromisoformat, required=True)
    ap.add_argument("--hasta", type=date.fromisoformat, required=True)
    ap.add_argument("--lineas", default="C1,C5")
    ap.add_argument("--festivos", default="", help="festivos extra, AAAA-MM-DD separados por comas")
    ap.add_argument("--min-observaciones", type=int, default=MIN_OBSERVATIONS)
    ap.add_argument("--dias-excluidos", type=Path, default=DEFAULT_EXCLUDED_DAYS_PATH,
                    help="JSON de días anómalos que no entran (defecto: la lista versionada)")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)

    try:
        lista = load_excluded_days(args.dias_excluidos)
    except ExcludedDaysError as exc:
        print(f"ERROR: lista de días excluidos inválida: {exc}", file=sys.stderr)
        return 2
    excluidos = frozenset(d.date for d in lista)
    en_periodo = sorted(d for d in excluidos if args.desde <= d <= args.hasta)
    lineas = frozenset(x.strip() for x in args.lineas.split(",") if x.strip())
    obs = [f.observacion() for f in leer_filas(args.origen, args.desde, args.hasta, excluidos)]
    if not obs:
        print("ERROR: no hay filas en el histórico para ese periodo", file=sys.stderr)
        return 1
    tabla = build_table(obs, lines=lineas, holidays=festivos(args.festivos),
                        generated_at=datetime.now(tz=UTC), min_observations=args.min_observaciones,
                        excluded_days=excluidos)
    motivos = {d.date: d.reason for d in lista}
    for d in en_periodo:
        print(f"día excluido (no entra en la mediana): {d.isoformat()} — {motivos[d]}", file=sys.stderr)
    raw = json.dumps(tabla, ensure_ascii=False, separators=(",", ":")).encode()
    # El worker lo validará igual: si no pasa aquí, no se escribe.
    comprobada = PredictionTable.from_bytes(raw)
    args.out.write_bytes(raw)
    print(json.dumps({"out": str(args.out), "bytes": len(raw), "entries": comprobada.entry_count,
                      "lines": sorted(comprobada.lines), "history": tabla["history"],
                      "excluded_days_skipped": [d.isoformat() for d in en_periodo],
                      "excluded_days_skipped_count": len(en_periodo)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
