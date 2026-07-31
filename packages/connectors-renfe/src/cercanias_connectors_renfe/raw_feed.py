"""Parsing defensivo del JSON crudo de ``vehicle_positions.json`` — Anexo A.1, A.7.

Traduce el JSON de Renfe a primitivas (``RawVehicleEntity`` del dominio) sin
aplicar todavía ningún filtro de núcleo ni normalización de negocio. Aísla
las trampas de A.1:

- anidamiento doble ``vehicle.vehicle.label``,
- ``timestamp`` como string,
- bloque ``position`` ausente,
- ``stopId`` ausente.

Cualquier entidad que no tenga ``vehicle`` o ``tripId`` se descarta de forma
explícita y se contabiliza en ``skipped`` (nunca en silencio).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cercanias_domain.normalize import RawVehicleEntity


@dataclass(frozen=True)
class SkippedEntity:
    entity_id: str | None
    reason: str


@dataclass(frozen=True)
class FeedParseResult:
    header_timestamp_raw: str
    entities: list[RawVehicleEntity]
    skipped: list[SkippedEntity] = field(default_factory=list)


def parse_vehicle_positions_payload(payload: dict[str, Any]) -> FeedParseResult:
    """Convierte el payload JSON crudo en una lista de ``RawVehicleEntity``."""
    header = payload.get("header") or {}
    header_timestamp_raw = header.get("timestamp")
    if header_timestamp_raw is None:
        raise ValueError("header.timestamp ausente: el feed no tiene la forma esperada (Anexo A.1)")

    entities: list[RawVehicleEntity] = []
    skipped: list[SkippedEntity] = []

    for raw_entity in payload.get("entity", []):
        entity_id = raw_entity.get("id")
        vehicle = raw_entity.get("vehicle")
        if vehicle is None:
            skipped.append(SkippedEntity(entity_id, "sin bloque 'vehicle'"))
            continue

        trip = vehicle.get("trip") or {}
        trip_id = trip.get("tripId")
        if not trip_id:
            skipped.append(SkippedEntity(entity_id, "sin 'trip.tripId'"))
            continue

        # A.1: anidamiento doble. El descriptor de vehiculo va en
        # vehicle.vehicle, NO en vehicle.label.
        vehicle_descriptor = vehicle.get("vehicle") or {}
        vehicle_id = vehicle_descriptor.get("id")
        if not vehicle_id:
            skipped.append(SkippedEntity(entity_id, "sin 'vehicle.vehicle.id'"))
            continue
        label = vehicle_descriptor.get("label")

        # A.7: entidad sin bloque 'position' -> el vehiculo existe pero no
        # reporta GPS. No debe romper el pipeline.
        position = vehicle.get("position")
        latitude = position.get("latitude") if position is not None else None
        longitude = position.get("longitude") if position is not None else None

        current_status_raw = vehicle.get("currentStatus")
        stop_id_raw = vehicle.get("stopId")

        # El timestamp por entidad deberia estar siempre presente segun A.1,
        # pero si faltase se cae al del header antes que romper el registro.
        timestamp_raw = vehicle.get("timestamp")
        if timestamp_raw is None:
            timestamp_raw = header_timestamp_raw

        entities.append(
            RawVehicleEntity(
                entity_id=entity_id or f"sin-id:{trip_id}",
                trip_id=trip_id,
                vehicle_id=str(vehicle_id),
                latitude=latitude,
                longitude=longitude,
                current_status_raw=current_status_raw,
                stop_id_raw=stop_id_raw,
                label=label,
                timestamp_raw=str(timestamp_raw),
            )
        )

    return FeedParseResult(
        header_timestamp_raw=str(header_timestamp_raw),
        entities=entities,
        skipped=skipped,
    )
