"""Puertos del dominio (interfaces). Los adaptadores concretos viven en
``cercanias_persistence`` y en los paquetes ``connectors-*``; el dominio
solo conoce estos ``Protocol``.
"""

from __future__ import annotations

from typing import Protocol

from cercanias_domain.bearing import GeoPoint
from cercanias_domain.models import TripStaticInfo, VehicleObservation


class CurrentStateRepository(Protocol):
    """Guarda la posición actual Y la inmediatamente anterior por ``vehicle.id``.

    Corrección C-4: "posición actual: sobrescritura" (§14.4 original) impide
    calcular el rumbo. Este puerto expone explícitamente ambos estados.
    """

    def get_previous_position(self, vehicle_id: str) -> GeoPoint | None:
        """Posición del snapshot anterior para este vehículo, si existe."""
        ...

    def get_previous_bearing(self, vehicle_id: str) -> float | None:
        """Último rumbo conocido para este vehículo, si existe."""
        ...

    def update(self, observation: VehicleObservation) -> None:
        """Registra ``observation`` como actual, desplazando la anterior a 'previous'."""
        ...


class StaticScheduleIndex(Protocol):
    """Índice construido a partir del GTFS estático (``trips.txt``, ``routes.txt``)."""

    def lookup_trip(self, trip_id: str) -> TripStaticInfo | None: ...

    def has_stop(self, stop_id: str) -> bool: ...
