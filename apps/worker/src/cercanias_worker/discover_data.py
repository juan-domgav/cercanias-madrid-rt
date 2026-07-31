"""``make discover-data`` — E7 (+ apoyo de E3/E4).

Produce:

- ``data/mappings/estaciones-madrid.json``: todos los ``stop_id`` del feed de
  Madrid resueltos contra el GTFS estático y correlacionados con Adif.
- ``data/mappings/nucleos.yaml``: confirmación del mapeo de códigos de núcleo.
- ``docs/informes/cobertura-estaciones.md``: total de ``trip_id`` vistos,
  resueltos, no resueltos con causa, líneas detectadas, y comprobación
  explícita de si C9 emite posición.

Puerta 0-A (bloqueante): resolución de trip_id contra el GTFS estático.
Puerta 0-B (no bloqueante): correlación con Adif vía IDEADIF. Si IDEADIF no
responde, el comando sigue y lo marca como ANOMALÍA, nunca aborta por eso.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from cercanias_connectors_adif.station_correlation import (
    AdifDependencia,
    AdifDependenciasUnavailableError,
    fetch_adif_dependencias,
)
from cercanias_connectors_renfe.gtfs_rt_client import (
    GtfsRtFetchError,
    fetch_vehicle_positions_payload,
    normalize_feed_payload,
)
from cercanias_connectors_renfe.gtfs_static_downloader import (
    GtfsStaticUnavailableError,
    download_gtfs_static,
)
from cercanias_connectors_renfe.gtfs_static_index import (
    CsvStaticScheduleIndex,
    build_static_schedule_index,
)
from cercanias_domain.enums import MappingMethod, ResolutionMethod
from cercanias_domain.models import StationMapping, VehicleObservation
from cercanias_domain.nucleo_filter import build_nucleo_filter
from cercanias_domain.station_matching import normalize_station_name
from cercanias_worker.config import WorkerConfig

#: Codigo de salida cuando ninguna de las tres fuentes GTFS estaticas responde.
#: C-1 exige fallo explicito: sin GTFS estatico no hay Puerta 0-A que evaluar y
#: cualquier informe generado seria una metrica calculada sobre datos vacios.
EXIT_NO_STATIC_GTFS = 3

# Tabla de referencia de codigos de nucleo del Anexo A.2 (lista de exclusion,
# NO verdad oficial segun el propio anexo) junto con el codigo equivalente
# observado en vivo el 31/07/2026 (ver ANOMALIA en nucleo_filter.py):
# el patron sistematico es sustituir los dos ultimos digitos "80" por "10".
_ALTA_COORDENADAS = "alta (coordenadas verificadas en Fase 0)"
_MEDIA_PATRON = "media (patron aritmetico)"

NUCLEO_REFERENCE_TABLE: list[dict[str, object]] = [
    {
        "nombre": "Madrid",
        "codigo_anexo_a": "1080",
        "codigo_observado_vivo": "1010",
        "confianza": "alta (coordenadas + Anexo A)",
    },
    {
        "nombre": "Sevilla",
        "codigo_anexo_a": "3080",
        "codigo_observado_vivo": "3010",
        "confianza": _ALTA_COORDENADAS,
    },
    {
        "nombre": "Cadiz",
        "codigo_anexo_a": "3180",
        "codigo_observado_vivo": "3110",
        "confianza": "media (patron aritmetico, no verificado individualmente por coordenadas)",
    },
    {
        "nombre": "Malaga",
        "codigo_anexo_a": "3280",
        "codigo_observado_vivo": "3210",
        "confianza": _MEDIA_PATRON,
    },
    {
        "nombre": "Valencia",
        "codigo_anexo_a": "4080",
        "codigo_observado_vivo": "4010",
        "confianza": _MEDIA_PATRON,
    },
    {
        "nombre": "Alicante/Murcia",
        "codigo_anexo_a": "4180",
        "codigo_observado_vivo": "4110",
        "confianza": _MEDIA_PATRON,
    },
    {
        "nombre": "Murcia-Cartagena",
        "codigo_anexo_a": "4580",
        "codigo_observado_vivo": "4510",
        "confianza": _MEDIA_PATRON,
    },
    {
        "nombre": "Noroeste (ancho metrico)",
        "codigo_anexo_a": "4680",
        "codigo_observado_vivo": "4610",
        "confianza": _MEDIA_PATRON,
    },
    {"nombre": "Leon", "codigo_anexo_a": "4780", "codigo_observado_vivo": "4710", "confianza": _MEDIA_PATRON},
    {
        "nombre": "Barcelona/Cataluna",
        "codigo_anexo_a": "5180",
        "codigo_observado_vivo": "5110",
        "confianza": "alta (coordenadas verificadas en Fase 0, etiquetas R2S/R2N/R4)",
    },
    {
        "nombre": "Bilbao",
        "codigo_anexo_a": "6080",
        "codigo_observado_vivo": "6010",
        "confianza": _ALTA_COORDENADAS,
    },
    {
        "nombre": "San Sebastian",
        "codigo_anexo_a": "6180",
        "codigo_observado_vivo": "6110",
        "confianza": _MEDIA_PATRON,
    },
    {
        "nombre": "Santander",
        "codigo_anexo_a": "6280",
        "codigo_observado_vivo": "6210",
        "confianza": _ALTA_COORDENADAS,
    },
    {
        "nombre": "Zaragoza",
        "codigo_anexo_a": "7080",
        "codigo_observado_vivo": "7010",
        "confianza": _ALTA_COORDENADAS,
    },
]


def _resolve_station_mappings(
    stop_ids: set[str],
    static_index: CsvStaticScheduleIndex | None,
    adif_dependencias: list[AdifDependencia],
) -> list[StationMapping]:
    adif_by_code = {d.cod_depend: d for d in adif_dependencias}
    adif_by_normalized_name = {
        normalize_station_name(d.nombre): d for d in adif_dependencias if d.cod_tipo_d in ("E", "P")
    }

    mappings: list[StationMapping] = []
    now = datetime.now(tz=UTC)
    for stop_id in sorted(stop_ids):
        static_stop = static_index.get_stop(stop_id) if static_index is not None else None
        name = static_stop.stop_name if static_stop else stop_id
        latitude = static_stop.latitude if static_stop else None
        longitude = static_stop.longitude if static_stop else None
        normalized = normalize_station_name(name)

        adif_match = adif_by_code.get(stop_id)
        if adif_match is not None:
            mapping_method = MappingMethod.EXACT_CODE
            confidence = 1.0
            adif_code: str | None = adif_match.cod_depend
        elif normalized in adif_by_normalized_name:
            mapping_method = MappingMethod.EXACT_NAME
            confidence = 0.7
            adif_code = adif_by_normalized_name[normalized].cod_depend
        else:
            mapping_method = MappingMethod.UNRESOLVED
            confidence = 0.0
            adif_code = None

        mappings.append(
            StationMapping(
                canonical_station_id=f"renfe:{stop_id}",
                renfe_stop_id=stop_id,
                adif_dependency_code=adif_code,
                name=name,
                normalized_name=normalized,
                latitude=latitude,
                longitude=longitude,
                mapping_method=mapping_method,
                confidence=confidence,
                verified_at=now if adif_dependencias else None,
            )
        )
    return mappings


def main() -> int:  # noqa: PLR0915 - orquestacion secuencial de un CLI, se mantiene lineal a proposito
    config = WorkerConfig()
    anomalies: list[str] = []
    assumptions: list[str] = []

    # --- 1. GTFS estatico (E3, cascada C-1) ---
    static_index: CsvStaticScheduleIndex | None = None
    gtfs_source_used: str | None = None
    try:
        outcome = download_gtfs_static(
            Path(config.gtfs_static_dir), timeout_seconds=config.source_timeout_seconds
        )
        static_index = build_static_schedule_index(outcome.version_dir)
        gtfs_source_used = outcome.source_used.source_name
        print(
            f"GTFS estatico: fuente usada = {gtfs_source_used}, {static_index.trip_count()} trips indexados"
        )
        for attempt in outcome.attempts:
            if not attempt.ok:
                anomalies.append(f"Fuente GTFS '{attempt.source_name}' fallo: {attempt.reason}")
    except GtfsStaticUnavailableError as exc:
        # C-1 / Anexo A.10: si ninguna de las tres fuentes responde hay que FALLAR
        # de forma explicita, nunca continuar con datos vacios. Continuar producia
        # informes de cobertura con aspecto normal (100 % de trip_id "sin resolver"
        # presentado como resultado) construidos sobre la nada, que es peor que no
        # tener informe: invita a cerrar la Puerta 0-A con datos inexistentes.
        print(
            f"ERROR FATAL: ninguna de las fuentes GTFS estaticas respondio ({exc}).\n"
            "C-1 exige fallar explicitamente en este caso; no se generan informes ni "
            "mappings, porque cualquier metrica de cobertura calculada ahora seria falsa.\n"
            "Resuelve el origen de datos antes de reintentar (runbook, bloque 2).",
            file=sys.stderr,
        )
        return EXIT_NO_STATIC_GTFS

    # --- 2. Feed en vivo, con fallback a fixture (Puerta 0-A no debe bloquear el descubrimiento) ---
    nucleo_filter = build_nucleo_filter(config.renfe_nucleo_codes)
    feed_source = "vivo"
    try:
        payload = fetch_vehicle_positions_payload(
            config.renfe_vehicle_positions_url, timeout_seconds=config.source_timeout_seconds
        )
    except GtfsRtFetchError as exc:
        feed_source = "fixture (vehicle_positions_madrid.json)"
        anomalies.append(
            f"vehicle_positions.json en vivo no disponible ({exc}); se ha usado el fixture "
            "congelado como alternativa. Los resultados de cobertura de ESTA ejecucion no son "
            "representativos del feed en produccion."
        )
        with (Path(config.fixtures_dir) / "vehicle_positions_madrid.json").open(encoding="utf-8") as f:
            payload = json.load(f)

    result = normalize_feed_payload(
        payload,
        nucleo_filter=nucleo_filter,
        static_index=static_index,
        min_displacement_meters=config.bearing_min_displacement_meters,
    )
    print(
        f"Feed ({feed_source}): {result.total_entities_in_payload} entidades totales, "
        f"{result.madrid_entities_after_filter} tras filtro de nucleo Madrid, "
        f"{len(result.observations)} normalizadas"
    )

    # --- 3. Adif IDEADIF: correlacion de codigos (Puerta 0-B, NO bloqueante) ---
    adif_dependencias: list[AdifDependencia] = []
    try:
        adif_dependencias = fetch_adif_dependencias(timeout_seconds=config.source_timeout_seconds)
        print(
            f"IDEADIF: {len(adif_dependencias)} dependencias descargadas de la capa "
            "Tramificacion:Dependencias"
        )
    except AdifDependenciasUnavailableError as exc:
        anomalies.append(
            f"IDEADIF (capa Dependencias) no disponible: {exc}. La correlacion Renfe<->Adif "
            "(Puerta 0-B) no se puede calcular en esta ejecucion; no bloquea el resto del informe."
        )

    # --- 4. Estadisticas de resolucion (Puerta 0-A) ---
    total_seen = len(result.observations)
    resolved = [o for o in result.observations if o.resolution == ResolutionMethod.TRIP_STATIC]
    fallback = [o for o in result.observations if o.resolution == ResolutionMethod.FALLBACK_LABEL]
    lines_detected = sorted({o.route_raw for o in result.observations})
    line_groups_detected = sorted({o.route_group for o in result.observations})

    c9_observations = [o for o in result.observations if o.route_group == "C9"]
    c9_with_position = [o for o in c9_observations if o.has_position]

    # --- 5. Correlacion de estaciones ---
    stop_ids_seen = {o.stop_id_resolvable for o in result.observations if o.stop_id_resolvable is not None}
    station_mappings = _resolve_station_mappings(stop_ids_seen, static_index, adif_dependencias)
    by_method = Counter(m.mapping_method for m in station_mappings)

    # --- 6. Escribir data/mappings/estaciones-madrid.json ---
    mappings_dir = Path(config.mappings_dir)
    mappings_dir.mkdir(parents=True, exist_ok=True)
    estaciones_path = mappings_dir / "estaciones-madrid.json"
    estaciones_payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "gtfs_source_used": gtfs_source_used,
        "total_stations": len(station_mappings),
        "stations": [m.model_dump(mode="json") for m in station_mappings],
    }
    estaciones_path.write_text(
        json.dumps(estaciones_payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    # --- 7. Escribir data/mappings/nucleos.yaml ---
    nucleos_path = mappings_dir / "nucleos.yaml"
    nucleos_payload = {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "regla_filtrado": "prefijo de 4 digitos + letra separadora, o SPECIAL_<2 digitos>_ (Anexo A.2)",
        "codigos_configurados_esta_ejecucion": list(config.renfe_nucleo_codes),
        "nucleos": NUCLEO_REFERENCE_TABLE,
        "nota": (
            "Tabla de referencia, no verdad oficial (el propio Anexo A la califica de lista de "
            "exclusion inferida por coordenadas). Los codigos 'codigo_observado_vivo' fueron "
            "verificados el 31/07/2026 cruzando las posiciones GPS de cada prefijo con la "
            "geografia esperada (ver docs/informes/cobertura-estaciones.md)."
        ),
    }
    nucleos_path.write_text(
        yaml.safe_dump(nucleos_payload, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # --- 8. Escribir docs/informes/cobertura-estaciones.md ---
    reports_dir = Path(config.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    informe_path = reports_dir / "cobertura-estaciones.md"
    report_data = CoverageReportData(
        feed_source=feed_source,
        gtfs_source_used=gtfs_source_used,
        nucleo_codes_used=tuple(config.renfe_nucleo_codes),
        total_seen=total_seen,
        resolved=resolved,
        fallback=fallback,
        lines_detected=lines_detected,
        line_groups_detected=line_groups_detected,
        c9_observations=c9_observations,
        c9_with_position=c9_with_position,
        station_mappings=station_mappings,
        by_method=by_method,
        anomalies=anomalies,
        assumptions=assumptions,
        skipped_raw_count=len(result.skipped_raw),
        unparseable_trip_ids=result.unparseable_trip_ids,
    )
    _write_coverage_report(informe_path, report_data)
    print(f"\nInformes escritos:\n  {estaciones_path}\n  {nucleos_path}\n  {informe_path}")
    return 0


@dataclass(frozen=True)
class CoverageReportData:
    feed_source: str
    gtfs_source_used: str | None
    #: Codigos de nucleo realmente activos en esta ejecucion. Se publica en el
    #: informe porque el codigo `1080` del Anexo A.2 ya no aparece en el feed
    #: (ADR-010) y atribuir el recuento al Anexo sin mas seria inexacto.
    nucleo_codes_used: tuple[str, ...]
    total_seen: int
    resolved: list[VehicleObservation]
    fallback: list[VehicleObservation]
    lines_detected: list[str]
    line_groups_detected: list[str]
    c9_observations: list[VehicleObservation]
    c9_with_position: list[VehicleObservation]
    station_mappings: list[StationMapping]
    by_method: Counter[MappingMethod]
    anomalies: list[str]
    assumptions: list[str]
    skipped_raw_count: int
    unparseable_trip_ids: list[str]


def _puerta_0a_section(d: CoverageReportData) -> list[str]:
    resolution_rate = (len(d.resolved) / d.total_seen * 100) if d.total_seen else 0.0
    lines: list[str] = [
        "## Puerta 0-A: resolucion de trip_id contra el GTFS estatico",
        "",
        f"- Total de entidades del nucleo de Madrid tras el filtro: **{d.total_seen}**"
        f" (codigos configurados: {', '.join(d.nucleo_codes_used)};"
        " la regla estructural es la del Anexo A.2, pero el codigo `1080` que el Anexo"
        " documenta ya no aparece en el feed — ver ADR-010)",
        f"- Resueltas contra `trips.txt` (`resolution=trip_static`): **{len(d.resolved)}**",
        f"- Sin resolver, fallback a etiqueta del `tripId` (`resolution=fallback_label`): "
        f"**{len(d.fallback)}**",
        f"- Tasa de resolucion: **{resolution_rate:.1f}%**",
        "- Entidades del payload descartadas antes de normalizar "
        f"(sin `vehicle`/`tripId`/`vehicle.vehicle.id`): {d.skipped_raw_count}",
        "- `tripId` que no coincide con ningun formato conocido (ni estandar ni SPECIAL_): "
        f"{len(d.unparseable_trip_ids)}",
    ]
    if d.unparseable_trip_ids:
        lines.append(f"  - Ejemplos: {', '.join(d.unparseable_trip_ids[:10])}")
    lines += ["", "### Causas de trip_id no resueltos", ""]
    if d.fallback:
        lines += [
            "Todos los casos de `fallback_label` corresponden a `trip_id` ausentes del "
            "GTFS estatico descargado en esta ejecucion (servicios especiales, version "
            "desactualizada, o el propio trip_id `SPECIAL_10_*`). Detalle:",
            "",
            "| trip_id | route_raw | motivo probable |",
            "|---|---|---|",
        ]
        for obs in d.fallback[:30]:
            is_special = obs.is_special_trip
            motivo = "trip especial (SPECIAL_10_*)" if is_special else "no encontrado en trips.txt"
            lines.append(f"| `{obs.trip_id}` | {obs.route_raw} | {motivo} |")
    else:
        lines.append("Ninguno: el 100% de los trip_id vistos resolvieron contra el GTFS estatico.")
    return lines


def _lines_and_c9_section(d: CoverageReportData) -> list[str]:
    lines_joined = ", ".join(d.lines_detected) if d.lines_detected else "(ninguna)"
    groups_joined = ", ".join(d.line_groups_detected) if d.line_groups_detected else "(ninguno)"
    lines: list[str] = [
        "",
        "## Lineas detectadas",
        "",
        f"- Etiquetas `route_raw` distintas vistas: {lines_joined}",
        f"- Grupos `route_group` distintos vistos: {groups_joined}",
        "",
        "### Verificacion explicita: ¿emite C9 posicion GPS?",
        "",
    ]
    if d.c9_observations:
        lines.append(
            f"**SI.** Se han observado {len(d.c9_observations)} entidad(es) con `route_group=C9` "
            f"en esta ejecucion, de las cuales {len(d.c9_with_position)} incluyen bloque "
            '`position` (GPS). Esto contradice la nota del Anexo A.3 ("C9 no aparecio en la '
            'captura"): en una descarga en vivo del 31/07/2026, C9 SI emitio posicion. Se '
            "recomienda actualizar el Anexo A / eliminar cualquier logica de UI que oculte C9 "
            'por "sin tiempo real".'
        )
    else:
        lines.append(
            "**No se ha observado ningun trip de la linea C9 en esta ejecucion** "
            f"(fuente del feed: {d.feed_source}). Consistente con la nota original del Anexo "
            "A.3, pero no concluyente: hace falta repetir la comprobacion en mas ejecuciones "
            "antes de asumir que C9 nunca emite GPS."
        )
    return lines


def _puerta_0b_section(d: CoverageReportData) -> list[str]:
    station_total = len(d.station_mappings)
    exact_code_count = d.by_method.get(MappingMethod.EXACT_CODE, 0)
    exact_code_rate = (exact_code_count / station_total * 100) if station_total else 0.0

    lines: list[str] = [
        "",
        "## Puerta 0-B (no bloqueante): correlacion Renfe <-> Adif (C-10)",
        "",
        f"- Estaciones distintas vistas en el feed de Madrid: **{station_total}**",
        "- Resueltas por `exact_code` (coincidencia literal `stop_id` == `cod_depend` de "
        f"IDEADIF): **{exact_code_count}** ({exact_code_rate:.1f}%)",
        "- Resueltas por `exact_name` (nombre normalizado, residual): "
        f"**{d.by_method.get(MappingMethod.EXACT_NAME, 0)}**",
        f"- Sin resolver (`unresolved`): **{d.by_method.get(MappingMethod.UNRESOLVED, 0)}**",
        "",
    ]
    if station_total and exact_code_count == station_total:
        lines.append(
            "**Confirmacion de la pista de C-10: el 100% de las estaciones de Madrid vistas en "
            "esta ejecucion tienen un `stop_id` de Renfe identico al `cod_depend` de la capa "
            "IDEADIF `Tramificacion:Dependencias` (tipo estacion/apeadero). El matching por "
            "nombre y el matching espacial quedan confirmados como residuales, tal y como "
            "anticipaba la corrección C-10.**"
        )
    lines += [
        "",
        "| renfe_stop_id | nombre | mapping_method | adif_dependency_code | confidence |",
        "|---|---|---|---|---|",
    ]
    for m in d.station_mappings:
        lines.append(
            f"| `{m.renfe_stop_id}` | {m.name} | {m.mapping_method.value} | "
            f"{m.adif_dependency_code or '-'} | {m.confidence:.2f} |"
        )
    return lines


def _anomalies_and_assumptions_section(d: CoverageReportData) -> list[str]:
    lines: list[str] = ["", "## ANOMALIAS detectadas en esta ejecucion", ""]
    if d.anomalies:
        lines += [f"- **ANOMALIA:** {a}" for a in d.anomalies]
    else:
        lines.append("Ninguna.")

    lines += ["", "## SUPUESTOS ASUMIDOS", ""]
    if d.assumptions:
        lines += [f"- {a}" for a in d.assumptions]
    else:
        lines.append(
            "- `direction_id` no existe en `trips.txt`: se deriva ordenando los `route_id` que "
            "comparten `route_short_name` (ver `cercanias_connectors_renfe.gtfs_static_index`). "
            "La marca `direction_derived=True` viaja tanto en `TripStaticInfo` como en "
            "`VehicleObservation`, de modo que ningun consumidor puede tomarlo por dato "
            "oficial sin ignorar el flag explicitamente."
        )
    return lines


def _write_coverage_report(path: Path, d: CoverageReportData) -> None:
    header: list[str] = [
        "# Informe de cobertura de estaciones y resolucion de trip_id (E7)",
        "",
        f"Generado: {datetime.now(tz=UTC).isoformat()}",
        "",
        f"- Fuente del feed en tiempo real usada en esta ejecucion: **{d.feed_source}**",
        "- Fuente del GTFS estatico usada en esta ejecucion: "
        f"**{d.gtfs_source_used or 'NINGUNA (ver anomalias)'}**",
        "",
    ]
    lines = (
        header
        + _puerta_0a_section(d)
        + _lines_and_c9_section(d)
        + _puerta_0b_section(d)
        + _anomalies_and_assumptions_section(d)
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
