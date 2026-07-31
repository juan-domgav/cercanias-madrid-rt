"""Adaptador en memoria de ``CurrentStateRepository`` (puerto del dominio).

Corrección C-4: guarda `{current, previous}` por ``vehicle.id`` para poder
calcular el rumbo (Anexo A.5). No usa Redis ni ningún backend persistente
(§2.2 / ADR-004): es intencionalmente volátil, apto para un único proceso
worker. La sustitución por DynamoDB/Firestore en fases posteriores solo
requiere una nueva implementación de este mismo puerto.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from cercanias_domain.bearing import GeoPoint
from cercanias_domain.models import VehicleObservation


@dataclass
class _VehicleState:
    current: VehicleObservation
    previous: VehicleObservation | None


class InMemoryCurrentStateRepository:
    """Implementa ``cercanias_domain.ports.CurrentStateRepository``."""

    def __init__(self) -> None:
        self._states: dict[str, _VehicleState] = {}
        self._lock = threading.Lock()

    def get_previous_position(self, vehicle_id: str) -> GeoPoint | None:
        with self._lock:
            state = self._states.get(vehicle_id)
            if state is None or not state.current.has_position:
                return None
            obs = state.current
            if obs.latitude is None or obs.longitude is None:
                return None
            return GeoPoint(latitude=obs.latitude, longitude=obs.longitude)

    def get_previous_bearing(self, vehicle_id: str) -> float | None:
        with self._lock:
            state = self._states.get(vehicle_id)
            if state is None:
                return None
            return state.current.bearing_degrees

    def update(self, observation: VehicleObservation) -> None:
        with self._lock:
            existing = self._states.get(observation.vehicle_id)
            previous = existing.current if existing is not None else None
            self._states[observation.vehicle_id] = _VehicleState(
                current=observation, previous=previous
            )

    def snapshot_size(self) -> int:
        with self._lock:
            return len(self._states)
