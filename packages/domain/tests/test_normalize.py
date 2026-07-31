from __future__ import annotations

from cercanias_domain.bearing import GeoPoint
from cercanias_domain.enums import BearingSource, ResolutionMethod, VehicleStatus
from cercanias_domain.models import TripStaticInfo, VehicleObservation
from cercanias_domain.normalize import RawVehicleEntity, normalize_entity, parse_feed_timestamp


class FakeStaticIndex:
    def __init__(self, trips: dict[str, TripStaticInfo], stops: set[str]) -> None:
        self._trips = trips
        self._stops = stops

    def lookup_trip(self, trip_id: str) -> TripStaticInfo | None:
        return self._trips.get(trip_id)

    def has_stop(self, stop_id: str) -> bool:
        return stop_id in self._stops


class FakeStateRepository:
    def __init__(self) -> None:
        self._previous_position: dict[str, GeoPoint] = {}
        self._previous_bearing: dict[str, float | None] = {}

    def get_previous_position(self, vehicle_id: str) -> GeoPoint | None:
        return self._previous_position.get(vehicle_id)

    def get_previous_bearing(self, vehicle_id: str) -> float | None:
        return self._previous_bearing.get(vehicle_id)

    def update(self, observation: VehicleObservation) -> None:
        if observation.has_position:
            assert observation.latitude is not None
            assert observation.longitude is not None
            self._previous_position[observation.vehicle_id] = GeoPoint(
                latitude=observation.latitude, longitude=observation.longitude
            )
        if observation.bearing_degrees is not None:
            self._previous_bearing[observation.vehicle_id] = observation.bearing_degrees


def _raw(**overrides: object) -> RawVehicleEntity:
    base: dict[str, object] = {
        "entity_id": "VP_C4a-20316",
        "trip_id": "1080J20316C4a",
        "vehicle_id": "20316",
        "latitude": 40.352676,
        "longitude": -3.684073,
        "current_status_raw": "INCOMING_AT",
        "stop_id_raw": "18000",
        "label": "C4a-20316-PLATF.(7)",
        "timestamp_raw": "1783019105",
    }
    base.update(overrides)
    return RawVehicleEntity(**base)  # type: ignore[arg-type]


def test_parse_feed_timestamp_string_to_epoch() -> None:
    dt = parse_feed_timestamp("1783019105")
    assert dt.isoformat() == "2026-07-02T19:05:05+00:00"


def test_normalize_without_static_index_uses_fallback_label() -> None:
    obs = normalize_entity(_raw(), static_index=None, state_repository=None)
    assert obs is not None
    assert obs.resolution == ResolutionMethod.FALLBACK_LABEL
    assert obs.route_id is None
    assert obs.direction_id is None
    assert obs.route_raw == "C4a"
    assert obs.route_group == "C4"
    assert obs.platform == "7"
    assert obs.current_status == VehicleStatus.INCOMING_AT


def test_normalize_resolves_against_static_index() -> None:
    static = FakeStaticIndex(
        trips={
            "1080J20316C4a": TripStaticInfo(
                route_id="10T0013C4a",
                route_short_name="C4a",
                direction_id=0,
                trip_headsign="El Escorial",
            )
        },
        stops={"18000"},
    )
    obs = normalize_entity(_raw(), static_index=static, state_repository=None)
    assert obs is not None
    assert obs.resolution == ResolutionMethod.TRIP_STATIC
    assert obs.route_id == "10T0013C4a"
    assert obs.direction_id == 0
    assert obs.trip_headsign == "El Escorial"


def test_normalize_unresolvable_trip_id_returns_none() -> None:
    obs = normalize_entity(_raw(trip_id="garbage"), static_index=None, state_repository=None)
    assert obs is None


def test_normalize_stop_id_sentinel_00000_is_not_resolvable() -> None:
    obs = normalize_entity(_raw(stop_id_raw="00000"), static_index=None, state_repository=None)
    assert obs is not None
    assert obs.stop_id_raw == "00000"
    assert obs.stop_id_resolvable is None


def test_normalize_missing_position_block() -> None:
    obs = normalize_entity(
        _raw(latitude=None, longitude=None, label="C4-23659"),
        static_index=None,
        state_repository=None,
    )
    assert obs is not None
    assert obs.has_position is False
    assert obs.latitude is None
    assert obs.longitude is None
    assert obs.bearing_degrees is None
    assert obs.bearing_source == BearingSource.NO_PREVIOUS_SNAPSHOT


def test_normalize_first_pass_bearing_is_none_second_pass_is_computed() -> None:
    repo = FakeStateRepository()
    first = normalize_entity(_raw(), static_index=None, state_repository=repo)
    assert first is not None
    assert first.bearing_degrees is None
    assert first.bearing_source == BearingSource.NO_PREVIOUS_SNAPSHOT

    moved = _raw(latitude=40.352676 + 0.002, timestamp_raw="1783019165")
    second = normalize_entity(moved, static_index=None, state_repository=repo)
    assert second is not None
    assert second.bearing_degrees is not None
    assert second.bearing_source == BearingSource.COMPUTED


def test_normalize_no_platform_in_label() -> None:
    obs = normalize_entity(_raw(label="C4-23659"), static_index=None, state_repository=None)
    assert obs is not None
    assert obs.platform is None


def test_normalize_unknown_current_status_does_not_raise() -> None:
    obs = normalize_entity(
        _raw(current_status_raw="SOME_FUTURE_VALUE"), static_index=None, state_repository=None
    )
    assert obs is not None
    assert obs.current_status == VehicleStatus.UNKNOWN
