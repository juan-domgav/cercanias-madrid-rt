"""Descarga puntual de la capa WFS "Dependencias" de IDEADIF — E7 (C-10).

IMPORTANTE: esto NO es "el conector de Adif" de fases posteriores (no
modela ``InfrastructureSegment``, no ingiere avisos, no tiene caché ni
programación). Es una consulta de investigación, acotada a una sola capa,
para responder la pregunta concreta de C-10 antes de escribir el algoritmo
de nombres: ¿los ``stop_id`` de 5 dígitos de Renfe coinciden con la
numeración de dependencias de Adif?

Descubierto en Fase 0 (ver ADR-009): la capa
``Tramificacion:Dependencias``, servida por
``https://ideadif.adif.es/gservices/Tramificacion/wfs``, tiene un campo
``cod_depend`` con formato alfanumérico distinto según ``cod_tipo_d``:

- ``cod_tipo_d = "E"`` (estación) y también ``"P"`` (apeadero) usan
  habitualmente un código NUMÉRICO de 5 dígitos que en la muestra
  descargada coincide exactamente con el ``stop_id`` de Renfe en el 98,6 %
  de los casos a nivel nacional y en el 100 % de los `stop_id` de Madrid
  vistos en una captura en vivo del feed (ver
  docs/informes/cobertura-estaciones.md).
- Otros tipos (``B`` bifurcación, ``F`` cambiador/infraestructura, ``K``
  agujas/límites de túnel, ``Z`` puestos de mando) usan códigos
  alfanuméricos con letra de prefijo (p. ej. ``B0602``), que NO coinciden
  con la numeración de Renfe y no se corresponden con estaciones.

Si esta consulta falla (IDEADIF no responde, cambia de estructura, etc.) se
degrada de forma explícita: ``discover-data`` debe seguir generando su
informe marcando esta comprobación como no disponible, nunca fallar todo el
comando por una fuente no bloqueante (Puerta 0-B).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

DEFAULT_TIMEOUT_SECONDS = 30.0

DEPENDENCIAS_WFS_URL = "https://ideadif.adif.es/gservices/Tramificacion/wfs"
DEPENDENCIAS_TYPE_NAME = "Tramificacion:Dependencias"

_NAMESPACE_STRIP_RE = re.compile(r"\{[^}]*\}")
_FIVE_DIGIT_RE = re.compile(r"^\d{5}$")


def _localname(tag: str) -> str:
    return _NAMESPACE_STRIP_RE.sub("", tag)


@dataclass(frozen=True)
class AdifDependencia:
    nombre: str
    cod_depend: str
    cod_tipo_d: str

    @property
    def looks_like_renfe_stop_id(self) -> bool:
        return bool(_FIVE_DIGIT_RE.fullmatch(self.cod_depend))


class AdifDependenciasUnavailableError(RuntimeError):
    """La capa de dependencias de IDEADIF no respondio o cambio de forma."""


def fetch_adif_dependencias(
    *,
    url: str = DEPENDENCIAS_WFS_URL,
    type_name: str = DEPENDENCIAS_TYPE_NAME,
    client: httpx.Client | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_features: int = 3200,
) -> list[AdifDependencia]:
    """Descarga (GetFeature, sin paginación adicional) la capa de dependencias completa.

    ``max_features`` cubre de sobra el volumen observado en Fase 0 (3085
    dependencias a nivel nacional).
    """
    if not url.startswith("https://"):
        raise ValueError("IDEADIF debe consultarse por https")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
    try:
        response = http_client.get(
            url,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeName": type_name,
                "propertyName": "nombre,cod_depend,cod_tipo_d",
                "count": str(max_features),
            },
        )
        response.raise_for_status()
        try:
            root = ET.fromstring(response.content)
        except ET.ParseError as exc:
            raise AdifDependenciasUnavailableError(
                f"Respuesta de {url} no es XML valido: {exc}"
            ) from exc
    except httpx.HTTPError as exc:
        raise AdifDependenciasUnavailableError(f"Fallo al consultar {url}: {exc}") from exc
    finally:
        if owns_client:
            http_client.close()

    results: list[AdifDependencia] = []
    for member in root:
        for feature in member:
            if _localname(feature.tag) != "Dependencias":
                continue
            fields = {_localname(c.tag): (c.text or "").strip() for c in feature}
            if "cod_depend" not in fields:
                continue
            results.append(
                AdifDependencia(
                    nombre=fields.get("nombre", ""),
                    cod_depend=fields["cod_depend"],
                    cod_tipo_d=fields.get("cod_tipo_d", ""),
                )
            )
    return results
