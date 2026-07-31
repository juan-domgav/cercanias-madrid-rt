"""WP-5 — conector de ``trip_updates`` (retrasos).

Hecho medido el 31/07/2026 sobre el feed real, y que condiciona todo lo que se
pueda construir encima:

- Madrid (núcleo 1010): 101 entidades ``tripUpdate``, 98 con ``stopTimeUpdate``
  (97,0 %). El retraso existe y su cobertura es buena.
- **Cada viaje trae UN SOLO ``stopTimeUpdate``**: 98 entidades → 98 valores de
  ``delay``. El feed informa del retraso respecto a la *próxima* parada, no de
  la secuencia completa del trayecto.

La consecuencia es de producto, no sólo técnica: no se puede responder
directamente «¿cuándo llega mi tren a MI estación?» si el usuario está diez
paradas más allá. Ese cálculo hay que derivarlo de ``stop_times.txt`` y marcarlo
como estimación (ver R-5 en la arquitectura v2). Este módulo se limita a extraer
fielmente lo que el feed publica; no estima nada.

El rango observado de retrasos fue de −120 s a +12 180 s (3 h 23 min).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

DEFAULT_TRIP_UPDATES_URL = "https://gtfsrt.renfe.com/trip_updates.json"
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Tope de tamaño de respuesta. El feed medido pesa ~115 KB; 25 MiB deja margen
#: de sobra sin permitir que un origen comprometido agote la memoria.
MAX_RESPONSE_BYTES = 25 * 1024 * 1024


class TripUpdatesFetchError(RuntimeError):
    """El feed de trip_updates no respondió o devolvió un cuerpo inesperado."""


@dataclass(frozen=True)
class TripDelay:
    """Retraso publicado para un viaje, en su próxima parada.

    ``delay_seconds`` es positivo si va con retraso y negativo si se adelanta
    (se observaron valores de hasta −120 s).
    """

    trip_id: str
    stop_id: str | None
    stop_sequence: int | None
    delay_seconds: int
    #: ``True`` si el retraso proviene de ``arrival``; ``False`` si de ``departure``.
    from_arrival: bool


@dataclass(frozen=True)
class TripUpdatesResult:
    total_entities: int
    matched_nucleo: int
    with_delay: int
    delays: tuple[TripDelay, ...]
    skipped_malformed: int

    @property
    def delay_coverage(self) -> float:
        return self.with_delay / self.matched_nucleo if self.matched_nucleo else 0.0

    def by_trip_id(self) -> dict[str, TripDelay]:
        return {d.trip_id: d for d in self.delays}


def fetch_trip_updates_payload(
    url: str = DEFAULT_TRIP_UPDATES_URL,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Descarga ``trip_updates.json`` con tope de tamaño y errores traducidos.

    A diferencia del cliente original de posiciones, aquí se traduce también el
    JSON inválido: un origen accesible pero corrupto debe activar el mismo
    camino de fallback que un origen caído, no escapar como ``JSONDecodeError``.
    """
    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds)
    try:
        response = http_client.get(url)
        response.raise_for_status()
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise TripUpdatesFetchError(
                f"{url} devolvio {len(response.content)} bytes, por encima del tope de "
                f"{MAX_RESPONSE_BYTES}"
            )
        payload = response.json()
    except httpx.HTTPError as exc:
        raise TripUpdatesFetchError(f"Fallo al descargar {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise TripUpdatesFetchError(f"{url} no devolvio JSON valido: {exc}") from exc
    finally:
        if owns_client:
            http_client.close()

    if not isinstance(payload, dict):
        raise TripUpdatesFetchError(
            f"{url} devolvio un JSON de tipo {type(payload).__name__}, se esperaba un objeto"
        )
    return payload


def _extract_delay(stop_time_update: dict[str, Any]) -> tuple[int, bool] | None:
    """Retraso de un ``stopTimeUpdate``, priorizando ``arrival`` sobre ``departure``."""
    for key, from_arrival in (("arrival", True), ("departure", False)):
        block = stop_time_update.get(key)
        if isinstance(block, dict):
            delay = block.get("delay")
            if isinstance(delay, int):
                return delay, from_arrival
    return None


def parse_trip_updates(
    payload: dict[str, Any], *, nucleo_prefixes: tuple[str, ...]
) -> TripUpdatesResult:
    """Extrae los retrasos del núcleo indicado, sin estimar nada.

    Parseo defensivo POR ENTIDAD (R-8): una entidad malformada se descarta y se
    cuenta, nunca tumba el lote entero.
    """
    entities = payload.get("entity")
    if not isinstance(entities, list):
        raise TripUpdatesFetchError(
            f"'entity' es {type(entities).__name__}, se esperaba una lista"
        )

    matched = 0
    skipped = 0
    delays: list[TripDelay] = []

    for entity in entities:
        if not isinstance(entity, dict):
            skipped += 1
            continue
        trip_update = entity.get("tripUpdate")
        if not isinstance(trip_update, dict):
            skipped += 1
            continue
        trip = trip_update.get("trip")
        if not isinstance(trip, dict):
            skipped += 1
            continue
        trip_id = trip.get("tripId")
        if not isinstance(trip_id, str) or not trip_id:
            skipped += 1
            continue
        if not trip_id.startswith(nucleo_prefixes):
            continue

        matched += 1
        found = False
        updates = trip_update.get("stopTimeUpdate")
        if isinstance(updates, list):
            for update in updates:
                if not isinstance(update, dict):
                    skipped += 1
                    continue
                extracted = _extract_delay(update)
                if extracted is None:
                    continue
                delay_seconds, from_arrival = extracted
                stop_id = update.get("stopId")
                stop_sequence = update.get("stopSequence")
                delays.append(
                    TripDelay(
                        trip_id=trip_id,
                        stop_id=stop_id if isinstance(stop_id, str) else None,
                        stop_sequence=stop_sequence if isinstance(stop_sequence, int) else None,
                        delay_seconds=delay_seconds,
                        from_arrival=from_arrival,
                    )
                )
                found = True
                break  # el feed publica una sola parada por viaje; no asumir mas

        # Segunda fuente, observada el 31/07/2026: `tripUpdate.delay` a nivel de
        # viaje, en paralelo al de `stopTimeUpdate.arrival.delay`. Cubre las
        # entidades que no traen stopTimeUpdate pero si retraso agregado.
        if not found:
            trip_level = trip_update.get("delay")
            if isinstance(trip_level, int):
                delays.append(
                    TripDelay(
                        trip_id=trip_id,
                        stop_id=None,
                        stop_sequence=None,
                        delay_seconds=trip_level,
                        from_arrival=False,
                    )
                )

    return TripUpdatesResult(
        total_entities=len(entities),
        matched_nucleo=matched,
        with_delay=len(delays),
        delays=tuple(delays),
        skipped_malformed=skipped,
    )
