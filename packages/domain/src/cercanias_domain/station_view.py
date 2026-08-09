"""Vista por estación: «¿qué trenes vienen hacia mi andén?».

Responde la pregunta del viajero, no la del operador. Un aviso de línea («la C4
lleva retrasos») no sirve a quien espera en Tres Cantos: necesita saber qué
trenes se acercan a SU estación y en qué sentido.

Dos decisiones que evitan depender de datos que no tenemos:

1. **No hace falta el horario.** Decir «el tren está en Chamartín, va hacia
   Colmenar, 6 minutos tarde» es accionable sin ``stop_times.txt``. Calcular la
   hora exacta de llegada exigiría el horario y la propagación del retraso
   (riesgo R-5), que es trabajo aparte. Se prefiere una respuesta útil hoy a una
   exacta más adelante.

2. **El sentido se deduce de la geometría, no de ``direction_id``.** El
   ``direction_id`` del GTFS de Renfe es una convención local inventada (R-2). En
   cambio, si la próxima parada de un tren es tu estación y el tren está al sur
   de ella, va hacia el norte. Eso se resuelve con una sola instantánea, sin
   historial y sin esperar a que el tren se mueva —cosa que importa, porque en
   una ventana de 30 s sólo 23 de 88 trenes se desplazaron lo suficiente como
   para inferir el rumbo por movimiento.

La heurística asume que las líneas de Cercanías son aproximadamente lineales
alrededor de la estación consultada. Vale para los ramales radiales de Madrid; no
valdría para una línea circular.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

EARTH_RADIUS_KM = 6371.0

#: Radio de búsqueda por defecto. Un tren a más de 30 km de la estación tardará
#: tanto en llegar que mostrarlo es ruido.
DEFAULT_SEARCH_RADIUS_KM = 30.0


class Approach(StrEnum):
    """Relación de un tren con la estación consultada."""

    NEXT_STOP = "next_stop"  # su proxima parada ES esta estacion
    APPROACHING = "approaching"  # se dirige hacia ella, pero para antes
    LEAVING = "leaving"  # ya la paso
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Station:
    stop_id: str
    name: str
    latitude: float
    longitude: float


@dataclass(frozen=True)
class TrainAtStation:
    trip_id: str
    route: str
    distance_km: float
    approach: Approach
    delay_seconds: int | None
    at_stop_id: str | None
    #: ``True`` si el tren está al sur de la estación. Combinado con
    #: :attr:`approach` da el sentido sin recurrir a ``direction_id``.
    is_south_of_station: bool
    platform: str | None

    @property
    def delay_minutes(self) -> int | None:
        return None if self.delay_seconds is None else self.delay_seconds // 60


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distancia sobre la superficie terrestre, en kilómetros."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


@dataclass(frozen=True)
class _Positioned:
    """Lo mínimo que se necesita de una observación para esta vista."""

    trip_id: str
    route: str
    latitude: float
    longitude: float
    next_stop_id: str | None
    platform: str | None


def nearest_stations(
    latitude: float, longitude: float, stations: list[Station], *, limit: int = 5
) -> tuple[tuple[Station, float], ...]:
    """Estaciones más cercanas a una posición, con su distancia.

    Se calcula en el cliente a partir del catálogo, que son unos pocos KB: la
    posición del usuario **no viaja al servidor**. Eso evita tratar un dato
    personal (RGPD) y mantiene la ruta de lectura como un fichero estático tras
    CDN, sin cómputo por usuario.
    """
    con_distancia = [
        (s, haversine_km(latitude, longitude, s.latitude, s.longitude)) for s in stations
    ]
    con_distancia.sort(key=lambda pair: pair[1])
    return tuple(con_distancia[:limit])


def trains_for_station(
    trains: list[_Positioned],
    station: Station,
    *,
    delays_by_trip: dict[str, int] | None = None,
    radius_km: float = DEFAULT_SEARCH_RADIUS_KM,
) -> tuple[TrainAtStation, ...]:
    """Trenes relevantes para una estación, ordenados por cercanía."""
    delays = delays_by_trip or {}
    resultado: list[TrainAtStation] = []

    for train in trains:
        distancia = haversine_km(train.latitude, train.longitude, station.latitude, station.longitude)
        if distancia > radius_km:
            continue

        if train.next_stop_id == station.stop_id:
            approach = Approach.NEXT_STOP
        elif train.next_stop_id is None:
            approach = Approach.UNKNOWN
        else:
            # Para otra parada: no se puede afirmar el sentido sin la topología
            # de la línea. Se marca como desconocido en lugar de adivinar.
            approach = Approach.UNKNOWN

        resultado.append(
            TrainAtStation(
                trip_id=train.trip_id,
                route=train.route,
                distance_km=round(distancia, 2),
                approach=approach,
                delay_seconds=delays.get(train.trip_id),
                at_stop_id=train.next_stop_id,
                is_south_of_station=train.latitude < station.latitude,
                platform=train.platform,
            )
        )

    # Los que tienen esta estación como próxima parada van primero: son los
    # únicos de los que se puede afirmar el sentido con certeza.
    resultado.sort(key=lambda t: (t.approach is not Approach.NEXT_STOP, t.distance_km))
    return tuple(resultado)
