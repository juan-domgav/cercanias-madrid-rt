from __future__ import annotations

import json
from pathlib import Path

import pytest

from cercanias_connectors_renfe.raw_feed import parse_vehicle_positions_payload

FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"


def _load(name: str) -> dict[str, object]:
    with (FIXTURES_DIR / name).open(encoding="utf-8") as f:
        result: dict[str, object] = json.load(f)
        return result


def test_parses_all_13_madrid_entities() -> None:
    payload = _load("vehicle_positions_madrid.json")
    result = parse_vehicle_positions_payload(payload)
    assert len(result.entities) == 13
    assert result.skipped == []
    assert result.header_timestamp_raw == "1783019105"


def test_parses_casos_limite_without_raising() -> None:
    payload = _load("vehicle_positions_casos_limite.json")
    result = parse_vehicle_positions_payload(payload)
    assert len(result.entities) == 6
    assert result.skipped == []


def test_missing_position_block_does_not_break() -> None:
    payload = _load("vehicle_positions_casos_limite.json")
    result = parse_vehicle_positions_payload(payload)
    entity = next(e for e in result.entities if e.entity_id == "VP_C4-23661")
    assert entity.latitude is None
    assert entity.longitude is None


def test_stop_id_00000_preserved_raw_not_joined() -> None:
    payload = _load("vehicle_positions_casos_limite.json")
    result = parse_vehicle_positions_payload(payload)
    entity = next(e for e in result.entities if e.entity_id == "VP_C4-23659")
    assert entity.stop_id_raw == "00000"


def test_vehicle_id_differs_from_trip_id_train_number() -> None:
    payload = _load("vehicle_positions_casos_limite.json")
    result = parse_vehicle_positions_payload(payload)
    entity = next(e for e in result.entities if e.entity_id == "VP_C6-14465")
    assert entity.trip_id == "4080J30449C6"
    assert entity.vehicle_id == "14465"


def test_double_nested_vehicle_label_is_read_correctly() -> None:
    payload = _load("vehicle_positions_madrid.json")
    result = parse_vehicle_positions_payload(payload)
    entity = next(e for e in result.entities if e.entity_id == "VP_C4a-20316")
    assert entity.label == "C4a-20316-PLATF.(7)"


def test_missing_header_timestamp_raises() -> None:
    with pytest.raises(ValueError, match=r"header\.timestamp"):
        parse_vehicle_positions_payload({"header": {}, "entity": []})


def test_entity_without_vehicle_block_is_skipped_not_raised() -> None:
    payload = {
        "header": {"timestamp": "123"},
        "entity": [{"id": "broken"}],
    }
    result = parse_vehicle_positions_payload(payload)
    assert result.entities == []
    assert len(result.skipped) == 1
    assert result.skipped[0].entity_id == "broken"


def test_entity_without_trip_id_is_skipped() -> None:
    payload = {
        "header": {"timestamp": "123"},
        "entity": [{"id": "broken", "vehicle": {"trip": {}}}],
    }
    result = parse_vehicle_positions_payload(payload)
    assert result.entities == []
    assert result.skipped[0].reason == "sin 'trip.tripId'"


def test_entity_without_vehicle_vehicle_id_is_skipped() -> None:
    payload = {
        "header": {"timestamp": "123"},
        "entity": [{"id": "broken", "vehicle": {"trip": {"tripId": "1080J1C1"}, "vehicle": {}}}],
    }
    result = parse_vehicle_positions_payload(payload)
    assert result.entities == []
    assert "vehicle.vehicle.id" in result.skipped[0].reason
