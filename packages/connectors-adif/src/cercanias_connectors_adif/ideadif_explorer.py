"""Explorador de capacidades IDEADIF — E6, corrección C-12.

Consulta ``GetCapabilities`` de WMS y de WFS sobre HTTPS (nunca HTTP) y
enumera las capas / ``typeNames`` disponibles. Este módulo NO implementa
ningún conector de datos: solo explora y reporta qué existe realmente, para
que ``docs/adr/ADR-009-ideadif.md`` documente el resultado real en vez de lo
asumido por la especificación base (que daba por hecho que WFS estaba
disponible).
"""

from __future__ import annotations

from dataclasses import dataclass
from xml.etree import ElementTree as ET

import httpx

from cercanias_connectors_adif._xml import iter_children_named, iter_descendants_named

DEFAULT_TIMEOUT_SECONDS = 20.0
HTTP_OK = 200

# Endpoints documentados públicamente en los metadatos INSPIRE de IDEADIF
# (ver C-12). Se consultan ambos servicios OGC estándar sin asumir cuál
# responde: ese es precisamente el objetivo del explorador.
WMS_CAPABILITIES_URL = "https://ideadif.adif.es/geoserver/wms?service=WMS&request=GetCapabilities"
WFS_CAPABILITIES_URL = "https://ideadif.adif.es/geoserver/wfs?service=WFS&request=GetCapabilities"


@dataclass(frozen=True)
class CapabilitiesResult:
    service: str
    url: str
    ok: bool
    status_code: int | None
    reason: str
    version: str | None
    layer_or_typenames: tuple[str, ...]


def _fetch_xml(url: str, service_label: str, client: httpx.Client) -> CapabilitiesResult:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        return CapabilitiesResult(
            service=service_label,
            url=url,
            ok=False,
            status_code=None,
            reason=f"error de red: {exc}",
            version=None,
            layer_or_typenames=(),
        )

    if response.status_code != HTTP_OK:
        return CapabilitiesResult(
            service=service_label,
            url=url,
            ok=False,
            status_code=response.status_code,
            reason=f"HTTP {response.status_code}",
            version=None,
            layer_or_typenames=(),
        )

    try:
        root = ET.fromstring(response.content)
    except ET.ParseError as exc:
        return CapabilitiesResult(
            service=service_label,
            url=url,
            ok=False,
            status_code=response.status_code,
            reason=f"respuesta no es XML valido ({exc}); probablemente una pagina de error HTML",
            version=None,
            layer_or_typenames=(),
        )

    version = root.attrib.get("version")

    names: list[str] = []
    if service_label == "WMS":
        for layer in iter_descendants_named(root, "Layer"):
            name_el = next(iter_children_named(layer, "Name"), None)
            if name_el is not None and name_el.text:
                names.append(name_el.text.strip())
    else:  # WFS
        for feature_type in iter_descendants_named(root, "FeatureType"):
            name_el = next(iter_children_named(feature_type, "Name"), None)
            if name_el is not None and name_el.text:
                names.append(name_el.text.strip())

    return CapabilitiesResult(
        service=service_label,
        url=url,
        ok=True,
        status_code=response.status_code,
        reason="OK",
        version=version,
        layer_or_typenames=tuple(names),
    )


@dataclass(frozen=True)
class IdeadifExplorationResult:
    wms: CapabilitiesResult
    wfs: CapabilitiesResult


def explore_ideadif_capabilities(
    *,
    wms_url: str = WMS_CAPABILITIES_URL,
    wfs_url: str = WFS_CAPABILITIES_URL,
    client: httpx.Client | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> IdeadifExplorationResult:
    """Consulta GetCapabilities de WMS y WFS. Nunca lanza: el resultado en sí
    documenta si cada servicio existe o no."""
    for url in (wms_url, wfs_url):
        if not url.startswith("https://"):
            raise ValueError(f"IDEADIF debe consultarse por https, no: {url}")

    owns_client = client is None
    http_client = client or httpx.Client(timeout=timeout_seconds, follow_redirects=True)
    try:
        wms_result = _fetch_xml(wms_url, "WMS", http_client)
        wfs_result = _fetch_xml(wfs_url, "WFS", http_client)
    finally:
        if owns_client:
            http_client.close()

    return IdeadifExplorationResult(wms=wms_result, wfs=wfs_result)
