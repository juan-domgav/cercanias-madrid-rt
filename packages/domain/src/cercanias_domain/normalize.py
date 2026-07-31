"""Pipeline de normalización de una entidad cruda del feed a ``VehicleObservation``.

Este módulo NO conoce el JSON de Renfe: recibe primitivas ya extraídas
(cadena de ``tripId``, posición opcional, etc.) para poder testear el
dominio sin depender de ningún parser HTTP. El parsing del JSON crudo
(incluyendo el anidamiento doble ``vehicle.vehicle.label`` de A.1) vive en
``cercanias_connectors_renfe``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cercanias_domain.bearing import DEFAULT_MIN_DISPLACEMENT_METERS, GeoPoint, compute_bearing
from cercanias_domain.enums import BearingSource, ResolutionMethod, VehicleStatus
from cercanias_domain.models import VehicleObservation
from cercanias_domain.nucleo_filter import NucleoFilter
from cercanias_domain.platform import parse_platform
from cercanias_domain.ports import CurrentStateRepository, StaticScheduleIndex
from cercanias_domain.trip_id import parse_trip_id

STOP_ID_SENTINEL_NO_STATION = "00000"
"""Valor observado (Anexo A.7) que indica "tren no asociado a estación
resoluble". No debe usarse para hacer join con ``stops.txt``."""


@dataclass(frozen=True)
class RawVehicleEntity:
    """Primitivas ya extraídas de una entidad del feed, previas a normalizar.

    ``label`` corresponde a ``vehicle.vehicle.label`` (el anidamiento doble
    de A.1), no a ``vehicle.label``.
    """

    entity_id: str
    trip_id: str
    vehicle_id: str
    latitude: float | None
    longitude: float | None
    current_status_raw: str | None
    stop_id_raw: str | None
    label: str | None
    timestamp_raw: str


def parse_feed_timestamp(timestamp_raw: str) -> datetime:
    """``header.timestamp``/``vehicle.timestamp`` llegan como STRING (Anexo A.1).

    Un modelo Pydantic con ``int`` sin coerción explícita fallaría o
    silenciaría el error; aquí se hace explícito con ``int(...)``.
    """
    epoch_seconds = int(timestamp_raw)
    return datetime.fromtimestamp(epoch_seconds, tz=UTC)


def normalize_entity(
    raw: RawVehicleEntity,
    *,
    static_index: StaticScheduleIndex | None,
    state_repository: CurrentStateRepository | None,
    min_displacement_meters: float = DEFAULT_MIN_DISPLACEMENT_METERS,
) -> VehicleObservation | None:
    """Normaliza una entidad ya filtrada por núcleo.

    Devuelve ``None`` solo si el ``trip_id`` no coincide con ningún formato
    conocido (ver ``trip_id.parse_trip_id``); en ese caso la entidad se
    descarta explícitamente y debe contabilizarse como anomalía, nunca
    ignorarse en silencio.
    """
    parsed = parse_trip_id(raw.trip_id)
    if parsed is None:
        return None

    has_position = raw.latitude is not None and raw.longitude is not None

    # --- A.4: derivación de route_id / direction_id ---
    static_info = static_index.lookup_trip(raw.trip_id) if static_index is not None else None
    if static_info is not None:
        route_id = static_info.route_id
        direction_id = static_info.direction_id
        trip_headsign = static_info.trip_headsign
        resolution = ResolutionMethod.TRIP_STATIC
    else:
        route_id = None
        direction_id = None
        trip_headsign = None
        resolution = ResolutionMethod.FALLBACK_LABEL

    # --- A.7: stopId "00000" no es una estación resoluble ---
    stop_id_resolvable: str | None
    if raw.stop_id_raw is None or raw.stop_id_raw == STOP_ID_SENTINEL_NO_STATION:
        stop_id_resolvable = None
    else:
        stop_id_resolvable = raw.stop_id_raw

    # --- A.6: andén ---
    platform = parse_platform(raw.label)

    # --- A.5: rumbo sobre snapshot anterior ---
    if has_position and state_repository is not None:
        previous_position = state_repository.get_previous_position(raw.vehicle_id)
        previous_bearing = state_repository.get_previous_bearing(raw.vehicle_id)
        bearing_result = compute_bearing(
            previous_position=previous_position,
            current_position=GeoPoint(latitude=raw.latitude, longitude=raw.longitude),  # type: ignore[arg-type]
            previous_bearing_degrees=previous_bearing,
            min_displacement_meters=min_displacement_meters,
        )
        bearing_degrees = bearing_result.bearing_degrees
        bearing_source = bearing_result.source
    else:
        bearing_degrees = None
        bearing_source = BearingSource.NO_PREVIOUS_SNAPSHOT

    observation = VehicleObservation(
        entity_id=raw.entity_id,
        vehicle_id=raw.vehicle_id,
        trip_id=raw.trip_id,
        nucleo_code=parsed.nucleo_code,
        nucleo_prefix=parsed.nucleo_prefix,
        is_special_trip=parsed.is_special,
        route_raw=parsed.route_raw,
        route_group=parsed.route_group,
        route_id=route_id,
        direction_id=direction_id,
        trip_headsign=trip_headsign,
        resolution=resolution,
        has_position=has_position,
        latitude=raw.latitude,
        longitude=raw.longitude,
        current_status=VehicleStatus.from_raw(raw.current_status_raw),
        stop_id_raw=raw.stop_id_raw,
        stop_id_resolvable=stop_id_resolvable,
        platform=platform,
        observed_at=parse_feed_timestamp(raw.timestamp_raw),
        bearing_degrees=bearing_degrees,
        bearing_source=bearing_source,
    )

    if state_repository is not None:
        state_repository.update(observation)

    return observation


def filter_madrid_entities(
    entities: list[RawVehicleEntity], nucleo_filter: NucleoFilter
) -> list[RawVehicleEntity]:
    """Aplica el filtro de núcleo (A.2/C-3). Nunca filtra por etiqueta de línea."""
    return [e for e in entities if nucleo_filter.matches(e.trip_id)]
