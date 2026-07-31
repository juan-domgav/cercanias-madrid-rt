"""Cálculo de rumbo (bearing) a partir del snapshot anterior — Anexo A.5 / C-4.

El feed NO trae ``bearing`` ni ``speed`` (Anexo A.1). El rumbo se calcula en
la aplicación comparando la posición actual con la inmediatamente anterior
del mismo ``vehicle.id``. Por debajo de un umbral mínimo de desplazamiento
(25 m por defecto, configurable via ``BEARING_MIN_DISPLACEMENT_METERS``) se
conserva el último rumbo conocido para evitar que el icono gire por ruido
GPS. Si no hay snapshot previo, el rumbo es ``None``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from cercanias_domain.enums import BearingSource

EARTH_RADIUS_METERS = 6_371_000.0
DEFAULT_MIN_DISPLACEMENT_METERS = 25.0


@dataclass(frozen=True)
class GeoPoint:
    latitude: float
    longitude: float


def haversine_distance_meters(a: GeoPoint, b: GeoPoint) -> float:
    """Distancia ortodrómica entre dos puntos, en metros."""
    phi1 = math.radians(a.latitude)
    phi2 = math.radians(b.latitude)
    d_phi = math.radians(b.latitude - a.latitude)
    d_lambda = math.radians(b.longitude - a.longitude)
    sin_dphi_2 = math.sin(d_phi / 2)
    sin_dlambda_2 = math.sin(d_lambda / 2)
    h = sin_dphi_2**2 + math.cos(phi1) * math.cos(phi2) * sin_dlambda_2**2
    h = min(1.0, max(0.0, h))
    return 2 * EARTH_RADIUS_METERS * math.asin(math.sqrt(h))


def azimuth_degrees(origin: GeoPoint, destination: GeoPoint) -> float:
    """Azimut (rumbo) de ``origin`` a ``destination`` en grados [0, 360)."""
    phi1 = math.radians(origin.latitude)
    phi2 = math.radians(destination.latitude)
    d_lambda = math.radians(destination.longitude - origin.longitude)
    x = math.sin(d_lambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(d_lambda)
    theta = math.atan2(x, y)
    return (math.degrees(theta) + 360.0) % 360.0


@dataclass(frozen=True)
class BearingResult:
    bearing_degrees: float | None
    source: BearingSource


def compute_bearing(
    *,
    previous_position: GeoPoint | None,
    current_position: GeoPoint,
    previous_bearing_degrees: float | None,
    min_displacement_meters: float = DEFAULT_MIN_DISPLACEMENT_METERS,
) -> BearingResult:
    """Calcula el rumbo de un vehículo dado su snapshot anterior.

    - Sin snapshot previo -> ``bearing = None`` (icono sin orientación).
    - Desplazamiento por debajo del umbral -> se conserva el último rumbo
      conocido (evita iconos girando por ruido GPS).
    - En otro caso -> azimut real entre ambas posiciones.
    """
    if previous_position is None:
        return BearingResult(bearing_degrees=None, source=BearingSource.NO_PREVIOUS_SNAPSHOT)

    displacement = haversine_distance_meters(previous_position, current_position)
    if displacement < min_displacement_meters:
        return BearingResult(
            bearing_degrees=previous_bearing_degrees,
            source=BearingSource.INSUFFICIENT_DISPLACEMENT,
        )

    bearing = azimuth_degrees(previous_position, current_position)
    return BearingResult(bearing_degrees=bearing, source=BearingSource.COMPUTED)
