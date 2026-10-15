"""GET con reintentos cortos para los feeds GTFS-RT de Renfe.

Un fallo transitorio (conexion cortada, 5xx) no debe perder la pasada entera:
se reintenta pocas veces y con espera corta. Los 4xx no se reintentan: no se
arreglan solos. Tampoco se reintenta un ``TimeoutException``: el presupuesto de
tiempo de la invocacion (3 pasadas + sleeps) no aguanta triplicar un timeout
de 15-30 s; reintentar ahi es la forma mas rapida de tumbar el job entero.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

DEFAULT_RETRIES = 2
DEFAULT_BACKOFF_SECONDS = 1.0
#: Por debajo de esto (4xx) el error es del cliente y no se arregla reintentando.
FIRST_SERVER_ERROR = 500


def get_with_retries(
    client: httpx.Client,
    url: str,
    *,
    retries: int = DEFAULT_RETRIES,
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """Hace ``GET`` y devuelve la respuesta 2xx; propaga el ultimo error httpx."""
    attempt = 0
    while True:
        try:
            response = client.get(url)
            response.raise_for_status()
            return response
        except httpx.TimeoutException:
            # Ya hemos consumido timeout_seconds: reintentar multiplica el coste
            # por el numero de pasadas del tick (ver docs/runbooks / DESPLIEGUE).
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < FIRST_SERVER_ERROR or attempt >= retries:
                raise
        except httpx.TransportError:
            if attempt >= retries:
                raise
        sleep(backoff_seconds * (2**attempt))
        attempt += 1
