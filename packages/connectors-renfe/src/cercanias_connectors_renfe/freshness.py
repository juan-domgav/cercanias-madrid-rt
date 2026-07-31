"""Medición de frescura del feed — E2 / Anexo A.8.

Regla explícita del Anexo A.8: **no implementar el umbral de 120 s a
ciegas**. Antes hay que medir el desfase real de ``header.timestamp``
contra el reloj propio, dos veces, separadas ~25 s, y registrar el
resultado. Este módulo hace exactamente esa medición; la decisión sobre el
umbral se toma leyendo el informe generado (``docs/informes/feed-freshness.md``),
no aquí.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from cercanias_connectors_renfe.gtfs_rt_client import (
    DEFAULT_TIMEOUT_SECONDS,
    DEFAULT_VEHICLE_POSITIONS_URL,
    GtfsRtFetchError,
    fetch_vehicle_positions_payload,
)

DEFAULT_WAIT_SECONDS = 25.0


@dataclass(frozen=True)
class FreshnessSample:
    now_utc: datetime
    header_timestamp_raw: str
    header_timestamp_epoch_utc: datetime
    delta_seconds: float
    """``now_utc - header_timestamp_epoch_utc``. Positivo = el header va por detras del reloj."""


@dataclass(frozen=True)
class FreshnessReport:
    sample_1: FreshnessSample
    sample_2: FreshnessSample
    wall_clock_elapsed_seconds: float
    header_timestamp_delta_seconds: int
    """``header2 - header1``. Debe aproximarse al tiempo real transcurrido si
    el publicador actualiza con la cadencia declarada (~20 s)."""
    feed_clock_skew_seconds: float
    """Métrica a exportar en producción (Anexo A.8): media de los dos deltas
    reloj-propio vs header, para detectar cambios de comportamiento del
    publicador entre despliegues."""


def _sample(url: str, client: httpx.Client, timeout_seconds: float) -> FreshnessSample:
    before = time.time()
    payload = fetch_vehicle_positions_payload(url, client=client, timeout_seconds=timeout_seconds)
    after = time.time()
    now_utc = datetime.fromtimestamp((before + after) / 2, tz=UTC)

    header = payload.get("header") or {}
    header_timestamp_raw = header.get("timestamp")
    if header_timestamp_raw is None:
        raise GtfsRtFetchError("header.timestamp ausente en la respuesta")

    header_epoch = int(header_timestamp_raw)
    header_dt = datetime.fromtimestamp(header_epoch, tz=UTC)
    delta = (now_utc - header_dt).total_seconds()

    return FreshnessSample(
        now_utc=now_utc,
        header_timestamp_raw=str(header_timestamp_raw),
        header_timestamp_epoch_utc=header_dt,
        delta_seconds=delta,
    )


def measure_feed_freshness(
    url: str = DEFAULT_VEHICLE_POSITIONS_URL,
    *,
    wait_seconds: float = DEFAULT_WAIT_SECONDS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FreshnessReport:
    """Descarga el feed dos veces, separadas ``wait_seconds``, y mide el desfase."""
    with httpx.Client(timeout=timeout_seconds) as client:
        sample_1 = _sample(url, client, timeout_seconds)
        time.sleep(wait_seconds)
        sample_2 = _sample(url, client, timeout_seconds)

    wall_clock_elapsed = (sample_2.now_utc - sample_1.now_utc).total_seconds()
    header_delta = int(sample_2.header_timestamp_raw) - int(sample_1.header_timestamp_raw)
    skew = (sample_1.delta_seconds + sample_2.delta_seconds) / 2

    return FreshnessReport(
        sample_1=sample_1,
        sample_2=sample_2,
        wall_clock_elapsed_seconds=wall_clock_elapsed,
        header_timestamp_delta_seconds=header_delta,
        feed_clock_skew_seconds=skew,
    )
