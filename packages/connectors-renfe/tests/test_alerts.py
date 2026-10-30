"""Contrato del conector de avisos (``alerts.json``), RB-014.

``alerts_congelado_2026-10-07.json`` es un RECORTE de la captura real del
27/10/2026 (feed congelado desde el 07/10 09:32 Madrid): los 6 avisos de
Madrid por linea, 3 de ascensores de Madrid por ``stopId`` y varios de otros
nucleos (Sevilla, Valencia, Bilbao) como adversarios del filtro. Fuente: Renfe,
"Incidencias y avisos", data.renfe.com, CC BY 4.0.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from cercanias_connectors_renfe import alerts
from cercanias_connectors_renfe.alerts import (
    AlertsFetchError,
    alerts_hash,
    fetch_alerts_payload,
    parse_madrid_alerts,
)

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures"


def _congelado() -> dict[str, Any]:
    with (FIXTURES / "alerts_congelado_2026-10-07.json").open(encoding="utf-8") as f:
        return json.load(f)  # type: ignore[no-any-return]


def _entidad(  # noqa: PLR0913 - constructor de fixtures con nombres explicitos
    aid: str, texto: str, *, routes: tuple[str, ...] = (), stops: tuple[str, ...] = (),
    start: str | None = "1791356520", end: str | None = None,
) -> dict[str, Any]:
    periodo: dict[str, str] = {}
    if start:
        periodo["start"] = start
    if end:
        periodo["end"] = end
    return {
        "id": aid,
        "alert": {
            "activePeriod": [periodo],
            "informedEntity": [{"routeId": r} for r in routes] + [{"stopId": s} for s in stops],
            "descriptionText": {"translation": [{"text": texto, "language": "es"}]},
        },
    }


def test_filtra_madrid_en_captura_real() -> None:
    result = parse_madrid_alerts(_congelado())
    ids = [a["id"] for a in result.alerts]
    assert ids == sorted(ids)
    assert set(ids) == {
        "AVISO_518335", "AVISO_518347", "AVISO_517064", "AVISO_516750", "AVISO_483553",
        "AVISO_467462", "INFO_518355", "INFO_518353", "INFO_518350",
    }
    assert result.total_entities == 15
    assert result.skipped_malformed == 0
    # header.timestamp 1791358372 = 07/10/2026 07:32:52 UTC (09:32 Madrid).
    assert result.feed_timestamp == datetime(2026, 10, 7, 7, 32, 52, tzinfo=UTC)


def test_normaliza_un_aviso_real() -> None:
    por_id = {a["id"]: a for a in parse_madrid_alerts(_congelado()).alerts}
    reajuste = por_id["AVISO_518335"]
    assert reajuste["tipo"] == "reajuste"
    assert reajuste["lines"] == ["C4", "C4a", "C4b"]
    assert reajuste["stops"] == []
    assert reajuste["start"] == "2026-10-07T09:02:00+02:00"
    assert reajuste["end"] is None
    assert reajuste["text"].startswith("#MadC4b Por reajuste de servicio")
    assert set(reajuste) == {"id", "start", "end", "lines", "stops", "text", "tipo"}
    assert por_id["AVISO_516750"]["tipo"] == "obras"
    # Rutas de C8 pero etiqueta #MadC9: se unen las dos fuentes.
    assert por_id["AVISO_467462"]["lines"] == ["C8", "C9"]
    ascensor = por_id["INFO_518353"]
    assert ascensor["stops"] == ["35001"]
    assert ascensor["lines"] == []
    assert ascensor["tipo"] == "otros"


def test_hashtag_madrid_sin_rutas_tambien_cuenta() -> None:
    payload = {"entity": [_entidad("A1", "#MadC2 Por avería en un tren, retrasos.")]}
    (aviso,) = parse_madrid_alerts(payload).alerts
    assert aviso["lines"] == ["C2"]
    assert aviso["tipo"] == "averia"


def test_descarta_otros_nucleos() -> None:
    payload = {
        "entity": [
            _entidad("S1", "Por obras", routes=("30T0001C1",)),
            _entidad("V1", "Ascensor fuera de servicio", stops=("64103",)),
        ]
    }
    assert parse_madrid_alerts(payload).alerts == ()


def test_stop_de_madrid_mezclado_solo_guarda_los_de_madrid() -> None:
    payload = {"entity": [_entidad("M1", "aviso", routes=("30T0001C1",), stops=("43000", "18002"))]}
    (aviso,) = parse_madrid_alerts(payload).alerts
    assert aviso["stops"] == ["18002"]


def test_end_y_start_ausentes_o_raros() -> None:
    payload = {
        "entity": [
            _entidad("A", "x", routes=("10T0001C5",), start=None, end="1791400000"),
            _entidad("B", "x", routes=("10T0001C5",), start="no-es-numero"),
        ]
    }
    a, b = parse_madrid_alerts(payload).alerts
    assert a["start"] is None
    assert a["end"] == "2026-10-07T21:06:40+02:00"
    assert b["start"] is None


def test_entidades_malformadas_se_cuentan_y_no_revientan() -> None:
    payload = {
        "header": {"timestamp": "no"},
        "entity": [
            "basura",
            {"id": "sin-alert"},
            {"id": 5, "alert": {}},
            {"id": "X", "alert": {"informedEntity": "no-lista"}},
            {"id": "Y", "alert": {"informedEntity": [7, {"routeId": 3}, {"routeId": "10T0001C3"}],
                                  "descriptionText": {"translation": "rara"}}},
            _entidad("OK", "#MadC3 Por obras", routes=("10T0001C3",)),
        ],
    }
    result = parse_madrid_alerts(payload)
    assert [a["id"] for a in result.alerts] == ["OK", "Y"]
    assert result.skipped_malformed == 3
    assert result.feed_timestamp is None
    y = result.alerts[1]
    assert y["text"] == ""
    assert y["tipo"] == "otros"


def test_sin_entity_es_feed_vacio_no_error() -> None:
    """GTFS-RT en JSON omite los repetidos vacios: sin avisos = sin clave."""
    result = parse_madrid_alerts({"header": {"timestamp": "1791358372"}})
    assert result.alerts == ()
    assert result.total_entities == 0


def test_entity_que_no_es_lista_es_error() -> None:
    with pytest.raises(AlertsFetchError):
        parse_madrid_alerts({"entity": {"no": "lista"}})


def test_traduccion_en_otro_idioma_si_no_hay_es() -> None:
    entidad = _entidad("A", "", routes=("10T0001C1",))
    entidad["alert"]["descriptionText"]["translation"] = [
        {"text": "Euskara", "language": "eu"},
        {"text": "Castellano por obras", "language": "es"},
    ]
    entidad2 = _entidad("B", "", routes=("10T0001C1",))
    entidad2["alert"]["descriptionText"]["translation"] = [{"text": "Solo uno"}]
    a, b = parse_madrid_alerts({"entity": [entidad, entidad2]}).alerts
    assert a["text"] == "Castellano por obras"
    assert b["text"] == "Solo uno"


def test_hash_estable_e_independiente_del_orden_y_de_la_cabecera() -> None:
    base = _congelado()
    h1 = alerts_hash(parse_madrid_alerts(base).alerts)
    otra = dict(base)
    otra["entity"] = list(reversed(base["entity"]))
    otra["header"] = {"timestamp": "1791999999"}
    assert alerts_hash(parse_madrid_alerts(otra).alerts) == h1
    cambiada = json.loads(json.dumps(base))
    cambiada["entity"][0]["alert"]["descriptionText"]["translation"][0]["text"] += " Actualizado."
    assert alerts_hash(parse_madrid_alerts(cambiada).alerts) != h1
    assert len(h1) == 64


def _cliente(respuesta: httpx.Response | Exception) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_ok() -> None:
    client = _cliente(httpx.Response(200, json={"header": {}, "entity": []}))
    assert fetch_alerts_payload(client=client) == {"header": {}, "entity": []}


@pytest.mark.parametrize(
    "respuesta",
    [
        httpx.Response(200, content=b"{no es json"),
        httpx.Response(200, json=[1, 2]),
        httpx.Response(404),
        httpx.ReadTimeout("lento"),
    ],
)
def test_fetch_traduce_errores(respuesta: httpx.Response | Exception) -> None:
    with pytest.raises(AlertsFetchError):
        fetch_alerts_payload(client=_cliente(respuesta), sleep=lambda _s: None)


def test_fetch_tope_de_tamano(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(alerts, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(AlertsFetchError, match="tope"):
        fetch_alerts_payload(client=_cliente(httpx.Response(200, json={"entity": [1, 2, 3]})))


def test_fetch_crea_y_cierra_su_cliente(monkeypatch: pytest.MonkeyPatch) -> None:
    creados: list[httpx.Client] = []
    original = httpx.Client

    def fabrica(**kwargs: Any) -> httpx.Client:
        assert kwargs["timeout"] == alerts.DEFAULT_TIMEOUT_SECONDS
        c = original(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))
        creados.append(c)
        return c

    monkeypatch.setattr(alerts.httpx, "Client", fabrica)
    assert fetch_alerts_payload() == {}
    assert creados[0].is_closed
