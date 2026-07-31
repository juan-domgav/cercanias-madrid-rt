from __future__ import annotations

import httpx
import pytest

from cercanias_connectors_adif.station_correlation import (
    AdifDependenciasUnavailableError,
    fetch_adif_dependencias,
)

SAMPLE_WFS_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
    xmlns:Tramificacion="https://ideadif.adif.es/gservices/Tramificacion"
    xmlns:gml="http://www.opengis.net/gml/3.2"
    numberMatched="3" numberReturned="3">
  <wfs:member>
    <Tramificacion:Dependencias gml:id="Dependencias.1">
      <Tramificacion:nombre>MADRID-CHAMARTIN-CLARA CAMPOAMOR</Tramificacion:nombre>
      <Tramificacion:cod_depend>17000</Tramificacion:cod_depend>
      <Tramificacion:cod_tipo_d>E</Tramificacion:cod_tipo_d>
    </Tramificacion:Dependencias>
  </wfs:member>
  <wfs:member>
    <Tramificacion:Dependencias gml:id="Dependencias.2">
      <Tramificacion:nombre>BIF. CHAMARTIN</Tramificacion:nombre>
      <Tramificacion:cod_depend>B1005</Tramificacion:cod_depend>
      <Tramificacion:cod_tipo_d>B</Tramificacion:cod_tipo_d>
    </Tramificacion:Dependencias>
  </wfs:member>
  <wfs:member>
    <Tramificacion:Dependencias gml:id="Dependencias.3">
      <Tramificacion:nombre>MADRID-ATOCHA CERCANIAS</Tramificacion:nombre>
      <Tramificacion:cod_depend>18000</Tramificacion:cod_depend>
      <Tramificacion:cod_tipo_d>E</Tramificacion:cod_tipo_d>
    </Tramificacion:Dependencias>
  </wfs:member>
</wfs:FeatureCollection>
"""


def test_fetch_adif_dependencias_parses_members() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=SAMPLE_WFS_RESPONSE.encode())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    result = fetch_adif_dependencias(client=client)

    assert len(result) == 3
    by_code = {d.cod_depend: d for d in result}
    assert by_code["17000"].nombre == "MADRID-CHAMARTIN-CLARA CAMPOAMOR"
    assert by_code["17000"].looks_like_renfe_stop_id is True
    assert by_code["B1005"].looks_like_renfe_stop_id is False


def test_fetch_adif_dependencias_rejects_http_url() -> None:
    with pytest.raises(ValueError, match="https"):
        fetch_adif_dependencias(url="http://ideadif.adif.es/gservices/Tramificacion/wfs")


def test_fetch_adif_dependencias_raises_on_invalid_xml() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not xml wfs</html><broken")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AdifDependenciasUnavailableError):
        fetch_adif_dependencias(client=client)


def test_fetch_adif_dependencias_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(AdifDependenciasUnavailableError):
        fetch_adif_dependencias(client=client)
