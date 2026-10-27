"""Conector del feed de avisos de Renfe (``alerts.json``), RB-014.

Fuente: Renfe, "Incidencias y avisos" (data.renfe.com), licencia CC BY 4.0.

Hechos medidos el 27/10/2026 que condicionan el diseno:

- El feed lleva CONGELADO desde el 07/10/2026 09:32 (Madrid): mismo
  ``header.timestamp`` y mismo ``Last-Modified``. Por eso el archivo de avisos
  se escribe solo cuando cambia el contenido (hash) y la antiguedad de la
  cabecera se vigila aparte.
- ~69 entidades, ~100 KB, de todos los nucleos. Solo ``activePeriod.start``;
  ``cause``/``effect``/``headerText`` vacios: el tipo sale del texto.

Este modulo descarga, filtra Madrid y normaliza. No escribe nada.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from cercanias_connectors_renfe.http_retry import DEFAULT_BACKOFF_SECONDS, get_with_retries
from cercanias_domain.avisos import (
    classify_tipo,
    is_madrid_route,
    is_madrid_stop,
    line_from_route_id,
    lines_from_hashtags,
)

DEFAULT_ALERTS_URL = "https://gtfsrt.renfe.com/alerts.json"
#: Corto a proposito: el aviso es accesorio y el tick tiene presupuesto fijo
#: (3 pasadas + sleeps). Un timeout no se reintenta (ver ``http_retry``).
DEFAULT_TIMEOUT_SECONDS = 5.0
#: Un solo reintento ante 5xx/conexion cortada: como mucho +1 s de espera.
DEFAULT_RETRIES = 1
#: El feed medido pesa ~100 KB.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MADRID_TZ = ZoneInfo("Europe/Madrid")


class AlertsFetchError(RuntimeError):
    """El feed de avisos no respondio o devolvio un cuerpo inesperado."""


@dataclass(frozen=True)
class AlertsResult:
    #: ``header.timestamp`` del feed (UTC) o ``None`` si falta o es ilegible.
    feed_timestamp: datetime | None
    #: Avisos de Madrid normalizados, ordenados por ``id``. Cada uno:
    #: ``{id, start, end, lines, stops, text, tipo}``.
    alerts: tuple[dict[str, Any], ...]
    total_entities: int
    skipped_malformed: int


def fetch_alerts_payload(
    url: str = DEFAULT_ALERTS_URL,
    *,
    client: httpx.Client | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    retries: int = DEFAULT_RETRIES,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Descarga ``alerts.json``; cualquier fallo sale como ``AlertsFetchError``."""
    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds)
    extra: dict[str, Any] = {"sleep": sleep} if sleep is not None else {}
    try:
        response = get_with_retries(
            http_client, url, retries=retries, backoff_seconds=DEFAULT_BACKOFF_SECONDS, **extra
        )
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise AlertsFetchError(
                f"{url} devolvio {len(response.content)} bytes, por encima del tope de {MAX_RESPONSE_BYTES}"
            )
        payload = response.json()
    except httpx.HTTPError as exc:
        raise AlertsFetchError(f"Fallo al descargar {url}: {exc!r}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AlertsFetchError(f"{url} no devolvio JSON valido: {exc}") from exc
    finally:
        if owns_client:
            http_client.close()
    if not isinstance(payload, dict):
        raise AlertsFetchError(
            f"{url} devolvio un JSON de tipo {type(payload).__name__}, se esperaba un objeto"
        )
    return payload


def _epoch(valor: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(valor), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso_madrid(valor: Any) -> str | None:
    momento = _epoch(valor)
    return momento.astimezone(MADRID_TZ).isoformat() if momento else None


def _texto(alert: dict[str, Any]) -> str:
    description = alert.get("descriptionText")
    translations = description.get("translation") if isinstance(description, dict) else None
    if not isinstance(translations, list):
        return ""
    validas = [t for t in translations if isinstance(t, dict) and isinstance(t.get("text"), str)]
    elegida = next((t for t in validas if t.get("language") == "es"), validas[0] if validas else None)
    return str(elegida["text"]).strip() if elegida else ""


def _normalize(aid: str, alert: dict[str, Any]) -> dict[str, Any] | None:
    """Aviso normalizado si afecta a Madrid; ``None`` si es de otro nucleo."""
    informed = alert.get("informedEntity")
    informed = [i for i in informed if isinstance(i, dict)] if isinstance(informed, list) else []
    routes = [r for i in informed if isinstance(r := i.get("routeId"), str) and is_madrid_route(r)]
    stops = sorted(
        {s for i in informed if isinstance(s := i.get("stopId"), str) and is_madrid_stop(s)}
    )
    texto = _texto(alert)
    etiquetas = lines_from_hashtags(texto)
    if not routes and not stops and not etiquetas:
        return None
    lineas = {linea for r in routes if (linea := line_from_route_id(r))} | set(etiquetas)
    periodos = alert.get("activePeriod")
    periodo = periodos[0] if isinstance(periodos, list) and periodos else {}
    periodo = periodo if isinstance(periodo, dict) else {}
    return {
        "id": aid,
        "start": _iso_madrid(periodo.get("start")),
        "end": _iso_madrid(periodo.get("end")),
        "lines": sorted(lineas),
        "stops": stops,
        "text": texto,
        "tipo": classify_tipo(texto),
    }


def parse_madrid_alerts(payload: dict[str, Any]) -> AlertsResult:
    """Filtra y normaliza los avisos de Madrid. Parseo defensivo por entidad.

    Un aviso es de Madrid si alguna entidad informada es una ruta ``10T...`` o
    una parada de Madrid, o si el texto lleva una etiqueta ``#MadCx``.
    """
    entities = payload.get("entity", [])
    if not isinstance(entities, list):
        raise AlertsFetchError(f"'entity' es {type(entities).__name__}, se esperaba una lista")
    header = payload.get("header")
    feed_ts = _epoch(header.get("timestamp")) if isinstance(header, dict) else None

    malformed = 0
    avisos: list[dict[str, Any]] = []
    for entity in entities:
        aid = entity.get("id") if isinstance(entity, dict) else None
        alert = entity.get("alert") if isinstance(entity, dict) else None
        if not isinstance(aid, str) or not aid or not isinstance(alert, dict):
            malformed += 1
            continue
        normalizado = _normalize(aid, alert)
        if normalizado is not None:
            avisos.append(normalizado)
    avisos.sort(key=lambda a: str(a["id"]))
    return AlertsResult(
        feed_timestamp=feed_ts,
        alerts=tuple(avisos),
        total_entities=len(entities),
        skipped_malformed=malformed,
    )


def alerts_hash(alerts: tuple[dict[str, Any], ...] | list[dict[str, Any]]) -> str:
    """SHA-256 del conjunto normalizado. No incluye la cabecera del feed: un feed
    que solo re-sella la hora sin cambiar avisos NO debe generar un objeto nuevo."""
    canon = json.dumps(
        sorted(alerts, key=lambda a: str(a.get("id"))),
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()
