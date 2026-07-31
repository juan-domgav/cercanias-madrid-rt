"""Configuración leída de variables de entorno — §25 + añadidos de las correcciones.

Ningún secreto se lee aquí (Fase 0 no usa Telegram ni tokens). Todas las
variables tienen un valor por defecto seguro para poder ejecutar
``make check-feed-freshness`` / ``make discover-data`` sin configurar nada.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from math import isfinite

from cercanias_domain.nucleo_filter import DEFAULT_NUCLEO_CODES, parse_nucleo_codes_env


def _getenv_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un número finito mayor que 0; se recibió {raw!r}") from exc
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} debe ser un número finito mayor que 0; se recibió {raw!r}")
    return value


@dataclass(frozen=True)
class WorkerConfig:
    renfe_vehicle_positions_url: str = field(
        default_factory=lambda: os.getenv(
            "RENFE_VEHICLE_POSITIONS_URL", "https://gtfsrt.renfe.com/vehicle_positions.json"
        )
    )
    renfe_nucleo_codes: tuple[str, ...] = field(
        default_factory=lambda: (
            parse_nucleo_codes_env(os.getenv("RENFE_NUCLEO_CODES")) or DEFAULT_NUCLEO_CODES
        )
    )
    stale_vehicle_seconds: float = field(
        default_factory=lambda: _getenv_float("STALE_VEHICLE_SECONDS", 120.0)
    )
    bearing_min_displacement_meters: float = field(
        default_factory=lambda: _getenv_float("BEARING_MIN_DISPLACEMENT_METERS", 25.0)
    )
    source_timeout_seconds: float = field(
        default_factory=lambda: _getenv_float("SOURCE_TIMEOUT_SECONDS", 20.0)
    )
    gtfs_static_dir: str = field(default_factory=lambda: os.getenv("GTFS_STATIC_DIR", "data/gtfs_static"))
    fixtures_dir: str = field(default_factory=lambda: os.getenv("FIXTURES_DIR", "fixtures"))
    reports_dir: str = field(default_factory=lambda: os.getenv("REPORTS_DIR", "docs/informes"))
    mappings_dir: str = field(default_factory=lambda: os.getenv("MAPPINGS_DIR", "data/mappings"))
