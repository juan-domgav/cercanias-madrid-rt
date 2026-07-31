"""Cliente GTFS-RT de Renfe (vehicle_positions.json) — E4.

Consume JSON (preferencia de diagnóstico) en vez de Protocol Buffers: el
``.pb`` no está verificado campo a campo (Anexo A.9, ADR-002); el JSON sí
(Anexo A completo). Se conserva la posibilidad de cambiar de formato sin
tocar el dominio, ya que este módulo solo produce ``RawVehicleEntity``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from cercanias_connectors_renfe.raw_feed import (
    FeedParseResult,
    SkippedEntity,
    parse_vehicle_positions_payload,
)
from cercanias_domain.models import VehicleObservation
from cercanias_domain.normalize import normalize_entity
from cercanias_domain.nucleo_filter import NucleoFilter
from cercanias_domain.ports import CurrentStateRepository, StaticScheduleIndex

DEFAULT_VEHICLE_POSITIONS_URL = "https://gtfsrt.renfe.com/vehicle_positions.json"
DEFAULT_TIMEOUT_SECONDS = 15.0


class GtfsRtFetchError(RuntimeError):
    """El feed GTFS-RT no respondió o devolvió un cuerpo inesperado."""


def fetch_vehicle_positions_payload(
    url: str = DEFAULT_VEHICLE_POSITIONS_URL,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Descarga y parsea como JSON el feed de posiciones de vehículos."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds)
    try:
        response = http_client.get(url)
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        return payload
    except httpx.HTTPError as exc:
        raise GtfsRtFetchError(f"Fallo al descargar {url}: {exc}") from exc
    finally:
        if owns_client:
            http_client.close()


@dataclass(frozen=True)
class NormalizedFeedResult:
    header_timestamp_raw: str
    observations: list[VehicleObservation]
    skipped_raw: list[SkippedEntity]
    unparseable_trip_ids: list[str]
    total_entities_in_payload: int
    madrid_entities_after_filter: int


def normalize_feed_payload(
    payload: dict[str, Any],
    *,
    nucleo_filter: NucleoFilter,
    static_index: StaticScheduleIndex | None = None,
    state_repository: CurrentStateRepository | None = None,
) -> NormalizedFeedResult:
    """Pipeline completo: parseo crudo -> filtro de núcleo -> normalización.

    Aplica, en este orden: A.1 (parsing defensivo), A.2/C-3 (filtro de
    núcleo, nunca por etiqueta de línea), A.4-A.7 (normalización completa por
    entidad vía ``cercanias_domain.normalize.normalize_entity``).
    """
    parsed: FeedParseResult = parse_vehicle_positions_payload(payload)
    madrid_entities = [e for e in parsed.entities if nucleo_filter.matches(e.trip_id)]

    observations: list[VehicleObservation] = []
    unparseable: list[str] = []
    for entity in madrid_entities:
        observation = normalize_entity(
            entity,
            static_index=static_index,
            state_repository=state_repository,
        )
        if observation is None:
            unparseable.append(entity.trip_id)
            continue
        observations.append(observation)

    return NormalizedFeedResult(
        header_timestamp_raw=parsed.header_timestamp_raw,
        observations=observations,
        skipped_raw=parsed.skipped,
        unparseable_trip_ids=unparseable,
        total_entities_in_payload=len(payload.get("entity", [])),
        madrid_entities_after_filter=len(madrid_entities),
    )
