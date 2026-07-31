"""Enumeraciones cerradas del dominio.

Ver Anexo A.7: ``currentStatus`` es un enum cerrado con una rama ``unknown``
que no debe romper el pipeline ante valores no vistos todavía.
"""

from __future__ import annotations

from enum import StrEnum


class VehicleStatus(StrEnum):
    """Valores observados de ``vehicle.currentStatus`` (Anexo A.7) más una
    rama de escape para valores futuros no documentados."""

    STOPPED_AT = "STOPPED_AT"
    INCOMING_AT = "INCOMING_AT"
    IN_TRANSIT_TO = "IN_TRANSIT_TO"
    UNKNOWN = "unknown"

    @classmethod
    def from_raw(cls, value: str | None) -> VehicleStatus:
        if value is None:
            return cls.UNKNOWN
        try:
            return cls(value)
        except ValueError:
            return cls.UNKNOWN


class ResolutionMethod(StrEnum):
    """Cómo se obtuvo ``route_id``/``direction_id`` para una observación.

    Ver Anexo A.4: el ``trip_id`` del feed en tiempo real se cruza siempre
    contra el GTFS estático; si no resuelve, se cae a la etiqueta de línea
    del propio ``tripId`` y el resultado se marca explícitamente.
    """

    TRIP_STATIC = "trip_static"
    FALLBACK_LABEL = "fallback_label"


class BearingSource(StrEnum):
    """Procedencia del valor de rumbo calculado (Anexo A.5 / corrección C-4)."""

    COMPUTED = "computed"
    INSUFFICIENT_DISPLACEMENT = "insufficient_displacement"
    NO_PREVIOUS_SNAPSHOT = "no_previous_snapshot"


class MappingMethod(StrEnum):
    """Método de correlación Renfe↔Adif para ``StationMapping`` (§5.1)."""

    EXACT_CODE = "exact_code"
    EXACT_NAME = "exact_name"
    UNRESOLVED = "unresolved"
