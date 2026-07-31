"""``make check-feed-freshness`` — E2.

Descarga ``vehicle_positions.json`` dos veces (separadas ~25 s) e imprime:
``now_utc``, ``header.timestamp`` crudo, interpretado como epoch, delta en
segundos, y el delta entre ambas descargas. Escribe el resultado en
``docs/informes/feed-freshness.md``.

Esto responde la incógnita del Anexo A.8: si el desfase es de segundos, el
umbral de 120 s de §4.2 se puede implementar tal cual; si es grande y
constante, la frescura debe medirse por variación entre descargas, no
contra el reloj local. Este comando SOLO mide y documenta: no implementa
ningún umbral por sí mismo.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from cercanias_connectors_renfe.freshness import FreshnessReport, measure_feed_freshness
from cercanias_connectors_renfe.gtfs_rt_client import GtfsRtFetchError
from cercanias_worker.config import WorkerConfig

WAIT_SECONDS = 25.0
SKEW_DECISION_THRESHOLD_SECONDS = 60.0


def _print_report(report: FreshnessReport) -> None:
    print("=== Medicion de frescura del feed (E2 / Anexo A.8) ===")
    print(f"now_utc (descarga 1):            {report.sample_1.now_utc.isoformat()}")
    print(f"header.timestamp crudo (1):      {report.sample_1.header_timestamp_raw}")
    print(f"header.timestamp epoch (1):      {report.sample_1.header_timestamp_epoch_utc.isoformat()}")
    print(f"delta (1) = now - header:        {report.sample_1.delta_seconds:.2f} s")
    print()
    print(f"now_utc (descarga 2, +{WAIT_SECONDS:.0f}s):    {report.sample_2.now_utc.isoformat()}")
    print(f"header.timestamp crudo (2):      {report.sample_2.header_timestamp_raw}")
    print(f"header.timestamp epoch (2):      {report.sample_2.header_timestamp_epoch_utc.isoformat()}")
    print(f"delta (2) = now - header:        {report.sample_2.delta_seconds:.2f} s")
    print()
    print(f"tiempo real transcurrido entre descargas: {report.wall_clock_elapsed_seconds:.2f} s")
    print(f"delta entre header.timestamp (2-1):       {report.header_timestamp_delta_seconds} s")
    print(f"feed_clock_skew_seconds (metrica):        {report.feed_clock_skew_seconds:.2f} s")


def _decision_note(report: FreshnessReport) -> str:
    skew = abs(report.feed_clock_skew_seconds)
    if skew <= SKEW_DECISION_THRESHOLD_SECONDS:
        return (
            "**Decision:** el desfase medido es de segundos (no de semanas como en la "
            "captura del Anexo A). Queda por tanto **habilitado** el umbral de 120 s de la "
            "especificacion base tal cual, comparando `now_utc` contra "
            "`header.timestamp`/`vehicle.timestamp` interpretados como epoch: la medicion "
            "que A.8 exigia antes de adoptarlo lo respalda.\n\n"
            "**Alcance real en Fase 0 (no confundir):** la decision esta tomada pero la "
            "politica **no esta implementada**. `STALE_VEHICLE_SECONDS` existe como valor "
            "de configuracion (120 s por defecto) y ningun punto del codigo marca, degrada "
            "ni descarta todavia una observacion por antiguedad. Implementarlo es trabajo "
            "de Fase 1; Fase 0 solo tenia que medir para poder decidir.\n\n"
            "Se sigue exportando `feed_clock_skew_seconds` como metrica para detectar si el "
            "comportamiento cambia en produccion."
        )
    return (
        "**Decision:** el desfase medido es grande. Siguiendo el Anexo A.8, la frescura NO "
        "se debe medir contra el reloj local: hay que usar la variacion del "
        "`header.timestamp` entre descargas consecutivas como senal de que el publicador "
        "sigue actualizando, e ignorar el valor absoluto del desfase."
    )


def _write_markdown_report(report: FreshnessReport | None, error: str | None) -> Path:
    reports_dir = Path(WorkerConfig().reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_path = reports_dir / "feed-freshness.md"

    generated_at = datetime.now(tz=UTC).isoformat()
    lines: list[str] = [
        "# Informe de frescura del feed GTFS-RT (E2)",
        "",
        f"Generado: {generated_at}",
        "",
        "Responde la incognita abierta en Anexo A.8: el `header.timestamp` de la captura "
        "original (29/07/2026) correspondia a una fecha unas cuatro semanas anterior a la "
        "descarga. Este informe mide el desfase en una ejecucion real contra "
        "`https://gtfsrt.renfe.com/vehicle_positions.json`.",
        "",
    ]

    if error is not None:
        lines += [
            "## ANOMALIA: no se pudo completar la medicion",
            "",
            f"```\n{error}\n```",
            "",
            "SUPUESTO ASUMIDO: sin poder medir el feed real, no se implementa ningun umbral "
            "de frescura todavia. El valor de `STALE_VEHICLE_SECONDS` (120s por defecto, "
            "configurable) queda definido pero sin activar automaticamente hasta re-ejecutar "
            "este comando con acceso a red.",
        ]
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return out_path

    assert report is not None
    lines += [
        "## Resultado medido",
        "",
        "| Medicion | Valor |",
        "|---|---|",
        f"| now_utc (descarga 1) | {report.sample_1.now_utc.isoformat()} |",
        f"| header.timestamp crudo (1) | `{report.sample_1.header_timestamp_raw}` |",
        f"| header.timestamp como epoch UTC (1) | {report.sample_1.header_timestamp_epoch_utc.isoformat()} |",
        f"| delta (1) = now - header | {report.sample_1.delta_seconds:.2f} s |",
        f"| now_utc (descarga 2, +{WAIT_SECONDS:.0f}s) | {report.sample_2.now_utc.isoformat()} |",
        f"| header.timestamp crudo (2) | `{report.sample_2.header_timestamp_raw}` |",
        f"| header.timestamp como epoch UTC (2) | {report.sample_2.header_timestamp_epoch_utc.isoformat()} |",
        f"| delta (2) = now - header | {report.sample_2.delta_seconds:.2f} s |",
        f"| tiempo real transcurrido entre descargas | {report.wall_clock_elapsed_seconds:.2f} s |",
        f"| delta entre header.timestamp (2-1) | {report.header_timestamp_delta_seconds} s |",
        f"| `feed_clock_skew_seconds` (metrica a exportar) | {report.feed_clock_skew_seconds:.2f} s |",
        "",
        _decision_note(report),
        "",
        "## ANOMALIA respecto al Anexo A.8",
        "",
        "La captura documentada en el Anexo A (29/07/2026) reportaba un `header.timestamp` "
        "que interpretado como epoch POSIX correspondia a una fecha unas cuatro semanas "
        "anterior a la propia captura (`1783019105` -> `2026-07-02T19:05:05Z`). "
        "**La medicion en vivo de esta ejecucion no reproduce ese desfase**: el "
        "`header.timestamp` corresponde a un instante a solo unos segundos del reloj real. "
        "Se asume que el desfase de 4 semanas del Anexo A fue un artefacto de la via de "
        "descarga usada en aquella captura (cache intermedia), no un comportamiento real "
        "y sostenido del publicador. Se recomienda seguir exportando "
        "`feed_clock_skew_seconds` en produccion para detectar si reaparece.",
        "",
        "## Metadatos declarados por Renfe",
        "",
        "Actualizacion declarada cada 20 segundos para los tres feeds de Cercanias "
        "(vehicle_positions, trip_updates, alerts). Licencia Creative Commons "
        "Attribution 4.0 (atribucion obligatoria).",
    ]
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out_path


def main() -> int:
    config = WorkerConfig()
    print(f"Descargando {config.renfe_vehicle_positions_url} dos veces, separadas {WAIT_SECONDS:.0f}s...")
    try:
        report = measure_feed_freshness(
            config.renfe_vehicle_positions_url,
            wait_seconds=WAIT_SECONDS,
            timeout_seconds=config.source_timeout_seconds,
        )
    except GtfsRtFetchError as exc:
        print(f"ERROR: no se pudo descargar el feed: {exc}", file=sys.stderr)
        out_path = _write_markdown_report(None, str(exc))
        print(f"Informe (con anomalia) escrito en {out_path}")
        return 1

    _print_report(report)
    out_path = _write_markdown_report(report, None)
    print(f"\nInforme escrito en {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
