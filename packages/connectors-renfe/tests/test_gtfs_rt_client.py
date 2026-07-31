from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cercanias_connectors_renfe.gtfs_rt_client import (
    GtfsRtFetchError,
    fetch_vehicle_positions_payload,
    normalize_feed_payload,
)
from cercanias_domain.enums import BearingSource
from cercanias_domain.nucleo_filter import build_nucleo_filter
from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"


def _load(name: str) -> dict[str, object]:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        result: dict[str, object] = json.load(f)
        return result


# --- Aserciones obligatorias del Anexo A.11 ---


def test_madrid_filter_returns_13_of_13_madrid_fixture() -> None:
    payload = _load("vehicle_positions_madrid.json")
    result = normalize_feed_payload(payload, nucleo_filter=build_nucleo_filter(["1080"]))
    assert result.total_entities_in_payload == 13
    assert result.madrid_entities_after_filter == 13
    assert len(result.observations) == 13


def test_madrid_filter_returns_0_of_6_on_casos_limite_fixture() -> None:
    payload = _load("vehicle_positions_casos_limite.json")
    result = normalize_feed_payload(payload, nucleo_filter=build_nucleo_filter(["1080"]))
    assert result.total_entities_in_payload == 6
    assert result.madrid_entities_after_filter == 0
    assert result.observations == []


def test_platform_0_returns_string_zero_not_none() -> None:
    payload = _load("vehicle_positions_madrid.json")
    result = normalize_feed_payload(payload, nucleo_filter=build_nucleo_filter(["1080"]))
    obs = next(o for o in result.observations if o.entity_id == "VP_C4b-20510")
    assert obs.platform == "0"


def test_platform_alphanumeric_does_not_raise() -> None:
    """PLATF.(8AM) esta en el fixture de casos limite (fuera de Madrid), pero
    debe parsear igualmente sin lanzar si se normaliza sin filtro de nucleo."""
    payload = _load("vehicle_positions_casos_limite.json")
    result = normalize_feed_payload(payload, nucleo_filter=build_nucleo_filter(["6280"]))
    assert result.madrid_entities_after_filter == 1
    obs = result.observations[0]
    assert obs.platform == "8AM"


def test_entity_without_position_excluded_from_map_but_not_broken() -> None:
    payload = _load("vehicle_positions_casos_limite.json")
    result = normalize_feed_payload(payload, nucleo_filter=build_nucleo_filter(["3080"]))
    obs_by_id = {o.entity_id: o for o in result.observations}
    assert obs_by_id["VP_C4-23661"].has_position is False


def test_stop_id_00000_does_not_join_with_stops_txt() -> None:
    payload = _load("vehicle_positions_casos_limite.json")
    result = normalize_feed_payload(payload, nucleo_filter=build_nucleo_filter(["3080"]))
    obs = next(o for o in result.observations if o.entity_id == "VP_C4-23659")
    assert obs.stop_id_raw == "00000"
    assert obs.stop_id_resolvable is None


def test_bearing_is_none_on_first_pass() -> None:
    payload = _load("vehicle_positions_madrid.json")
    repo = InMemoryCurrentStateRepository()
    result = normalize_feed_payload(
        payload, nucleo_filter=build_nucleo_filter(["1080"]), state_repository=repo
    )
    assert all(o.bearing_degrees is None for o in result.observations)
    assert all(o.bearing_source == BearingSource.NO_PREVIOUS_SNAPSHOT for o in result.observations)


def test_bearing_is_numeric_on_second_pass_with_displaced_fixture() -> None:
    repo = InMemoryCurrentStateRepository()
    nucleo_filter = build_nucleo_filter(["1080"])

    first_payload = _load("vehicle_positions_madrid.json")
    normalize_feed_payload(first_payload, nucleo_filter=nucleo_filter, state_repository=repo)

    second_payload = _load("vehicle_positions_madrid_desplazado.json")
    second_result = normalize_feed_payload(
        second_payload, nucleo_filter=nucleo_filter, state_repository=repo
    )

    assert len(second_result.observations) == 13
    assert all(o.bearing_degrees is not None for o in second_result.observations)
    assert all(o.bearing_source == BearingSource.COMPUTED for o in second_result.observations)
    # Desplazamiento ~200m al norte -> rumbo cercano a 0 grados.
    for obs in second_result.observations:
        assert obs.bearing_degrees is not None
        assert obs.bearing_degrees < 5 or obs.bearing_degrees > 355


def test_fetch_vehicle_positions_payload_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"header": {"timestamp": "1"}, "entity": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    payload = fetch_vehicle_positions_payload("https://example.test/vp.json", client=client)
    assert payload == {"header": {"timestamp": "1"}, "entity": []}


def test_fetch_vehicle_positions_payload_http_error_raises_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GtfsRtFetchError):
        fetch_vehicle_positions_payload("https://example.test/vp.json", client=client)
