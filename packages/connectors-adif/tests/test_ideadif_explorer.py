from __future__ import annotations

import httpx
import pytest

from cercanias_connectors_adif.ideadif_explorer import explore_ideadif_capabilities

WMS_CAPABILITIES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<WMS_Capabilities version="1.3.0" xmlns="http://www.opengis.net/wms">
  <Capability>
    <Layer>
      <Layer><Name>TN.RailTransportNetwork.RailwayLink</Name></Layer>
      <Layer><Name>TN.RailTransportNetwork.RailwayStationNode</Name></Layer>
    </Layer>
  </Capability>
</WMS_Capabilities>
"""

WFS_CAPABILITIES_XML = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:WFS_Capabilities version="2.0.0" xmlns:wfs="http://www.opengis.net/wfs/2.0">
  <wfs:FeatureTypeList>
    <wfs:FeatureType><wfs:Name>Tramificacion:Dependencias</wfs:Name></wfs:FeatureType>
    <wfs:FeatureType><wfs:Name>Tramificacion:TramosServicio</wfs:Name></wfs:FeatureType>
  </wfs:FeatureTypeList>
</wfs:WFS_Capabilities>
"""


def test_explore_ideadif_capabilities_parses_layers_and_typenames() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "service=WMS" in str(request.url) or "/wms" in str(request.url):
            return httpx.Response(200, content=WMS_CAPABILITIES_XML.encode())
        return httpx.Response(200, content=WFS_CAPABILITIES_XML.encode())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = explore_ideadif_capabilities(
        wms_url="https://example.test/wms?request=GetCapabilities",
        wfs_url="https://example.test/wfs?request=GetCapabilities",
        client=client,
    )

    assert result.wms.ok is True
    assert result.wms.version == "1.3.0"
    assert "TN.RailTransportNetwork.RailwayLink" in result.wms.layer_or_typenames

    assert result.wfs.ok is True
    assert result.wfs.version == "2.0.0"
    assert "Tramificacion:Dependencias" in result.wfs.layer_or_typenames


def test_explore_ideadif_capabilities_handles_html_404_gracefully() -> None:
    """Regresion del hallazgo real: un endpoint adivinado incorrectamente
    devuelve 404 con una SPA en HTML, no debe lanzar."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"<html>not found</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = explore_ideadif_capabilities(
        wms_url="https://example.test/wms", wfs_url="https://example.test/wfs", client=client
    )
    assert result.wms.ok is False
    assert result.wms.status_code == 404
    assert result.wfs.ok is False


def test_explore_ideadif_capabilities_rejects_http_url() -> None:
    with pytest.raises(ValueError, match="https"):
        explore_ideadif_capabilities(wms_url="http://example.test/wms")
