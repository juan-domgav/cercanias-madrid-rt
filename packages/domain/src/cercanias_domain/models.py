"""Modelos de dominio (Pydantic 2).

Estos modelos representan el resultado NORMALIZADO de aplicar las reglas del
Anexo A sobre el feed crudo. Nunca representan el JSON tal cual llega de
Renfe: ese parsing crudo vive en ``cercanias_connectors_renfe``.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from cercanias_domain.enums import BearingSource, MappingMethod, ResolutionMethod, VehicleStatus


class TripStaticInfo(BaseModel):
    """Información resuelta de un ``trip_id`` contra el GTFS estático.

    ANOMALÍA (ver docs/adr y cobertura-estaciones.md): el ``trips.txt``
    servido por Renfe en la cascada de fuentes de C-1 NO tiene columna
    ``direction_id`` (a diferencia de lo asumido en la especificación base y
    en A.4). En su lugar, cada sentido de una línea es un ``route_id``
    distinto (p. ej. "10T0001C1" Príncipe Pío→Aeropuerto y "10T0002C1"
    Aeropuerto→Príncipe Pío). ``direction_id`` se deriva por convención en el
    índice estático (ver ``cercanias_connectors_renfe.gtfs_static_index``):
    se ordenan los ``route_id`` que comparten ``route_short_name`` y se
    numeran 0/1. Se marca como derivado, nunca como dato literal del CSV.
    """

    model_config = ConfigDict(frozen=True)

    route_id: str
    route_short_name: str
    direction_id: int | None
    trip_headsign: str | None
    direction_derived: bool = True


class VehicleObservation(BaseModel):
    """Observación normalizada de una posición de vehículo GTFS-RT.

    Ver Anexo A completo. Campos ausentes en el feed (``bearing``, ``speed``,
    ``route_id`` a nivel de vehículo, ``congestionLevel``) simplemente no
    existen en este modelo: no se prometen.
    """

    model_config = ConfigDict(frozen=True)

    entity_id: str
    vehicle_id: str
    trip_id: str

    nucleo_code: str | None
    nucleo_prefix: str
    is_special_trip: bool

    route_raw: str
    route_group: str
    route_id: str | None
    direction_id: int | None
    #: ``True`` si ``direction_id`` es una convención local derivada, no un dato
    #: oficial. El GTFS estático de Renfe NO publica columna ``direction_id``
    #: (contra lo que asumen A.4 y C-5), así que se deriva ordenando los
    #: ``route_id`` que comparten ``route_short_name``. Sin este campo el
    #: consumidor no puede distinguir un valor inventado de uno oficial y acaba
    #: presentando como dirección confirmada algo que no lo es.
    direction_derived: bool = False
    trip_headsign: str | None
    resolution: ResolutionMethod

    has_position: bool
    latitude: float | None
    longitude: float | None

    current_status: VehicleStatus

    stop_id_raw: str | None
    """Valor tal cual venía en el feed. Puede ser "00000" (A.7): NUNCA usar
    ese valor para hacer join con stops.txt ni para inferir posición."""

    stop_id_resolvable: str | None
    """``stop_id_raw`` cuando es un identificador de estación real; ``None``
    si viene ausente o es el centinela "00000"."""

    platform: str | None

    observed_at: datetime
    """Derivado de ``vehicle.timestamp`` (string -> int -> epoch UTC)."""

    bearing_degrees: float | None
    bearing_source: BearingSource


class StationMapping(BaseModel):
    """Tabla de equivalencias Renfe↔Adif — §5.1, corrección C-10."""

    model_config = ConfigDict(frozen=True)

    canonical_station_id: str
    renfe_stop_id: str
    adif_dependency_code: str | None
    name: str
    normalized_name: str
    latitude: float | None
    longitude: float | None
    mapping_method: MappingMethod
    confidence: float
    verified_at: datetime | None
