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
        """Ultima posicion conocida, cayendo a ``previous`` si ``current`` no trae GPS.

        El par ``{current, previous}`` de C-4 existe precisamente para esto: un
        frame sin bloque ``position`` (caso limite del Anexo A.7, que ocurre en
        el feed real) no debe romper la cadena de rumbo. Mirar solo ``current``
        haria que el siguiente frame con GPS volviese a ``NO_PREVIOUS_SNAPSHOT``
        y se perdiese el ultimo rumbo conocido, justo lo que C-4 prohibe.
        """
        with self._lock:
            state = self._states.get(vehicle_id)
            if state is None:
                return None
            for obs in (state.current, state.previous):
                if obs is None or not obs.has_position:
                    continue
                if obs.latitude is None or obs.longitude is None:
                    continue
                return GeoPoint(latitude=obs.latitude, longitude=obs.longitude)
            return None

    def get_previous_bearing(self, vehicle_id: str) -> float | None:
        """Ultimo rumbo conocido, cayendo a ``previous`` si ``current`` no lo tiene.

        Misma razon que en :meth:`get_previous_position`: C-4 exige conservar el
        ultimo rumbo conocido cuando no se puede calcular uno nuevo.
        """
        with self._lock:
            state = self._states.get(vehicle_id)
            if state is None:
                return None
            for obs in (state.current, state.previous):
                if obs is not None and obs.bearing_degrees is not None:
                    return obs.bearing_degrees
            return None

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
