from __future__ import annotations

import math

from cercanias_domain.bearing import (
    GeoPoint,
    azimuth_degrees,
    compute_bearing,
    haversine_distance_meters,
)
from cercanias_domain.enums import BearingSource


def test_haversine_zero_distance() -> None:
    p = GeoPoint(latitude=40.4168, longitude=-3.7038)
    assert haversine_distance_meters(p, p) == 0.0


def test_haversine_known_distance_madrid_to_atocha_chamartin() -> None:
    chamartin = GeoPoint(latitude=40.4711789, longitude=-3.6829524)
    atocha = GeoPoint(latitude=40.4066191, longitude=-3.6894391)
    distance = haversine_distance_meters(chamartin, atocha)
    # ~7.2 km en linea recta entre ambas estaciones
    assert 6_500 < distance < 8_000


def test_azimuth_due_north_is_zero() -> None:
    origin = GeoPoint(latitude=40.0, longitude=-3.0)
    north = GeoPoint(latitude=40.01, longitude=-3.0)
    bearing = azimuth_degrees(origin, north)
    assert math.isclose(bearing, 0.0, abs_tol=0.5)


def test_azimuth_due_east_is_90() -> None:
    origin = GeoPoint(latitude=40.0, longitude=-3.0)
    east = GeoPoint(latitude=40.0, longitude=-2.99)
    bearing = azimuth_degrees(origin, east)
    assert math.isclose(bearing, 90.0, abs_tol=0.5)


def test_compute_bearing_no_previous_snapshot_is_none() -> None:
    result = compute_bearing(
        previous_position=None,
        current_position=GeoPoint(latitude=40.0, longitude=-3.0),
        previous_bearing_degrees=None,
    )
    assert result.bearing_degrees is None
    assert result.source == BearingSource.NO_PREVIOUS_SNAPSHOT


def test_compute_bearing_below_threshold_keeps_previous_bearing() -> None:
    origin = GeoPoint(latitude=40.0, longitude=-3.0)
    barely_moved = GeoPoint(latitude=40.00001, longitude=-3.0)  # ~1.1 m
    result = compute_bearing(
        previous_position=origin,
        current_position=barely_moved,
        previous_bearing_degrees=123.4,
        min_displacement_meters=25.0,
    )
    assert result.source == BearingSource.INSUFFICIENT_DISPLACEMENT
    assert result.bearing_degrees == 123.4


def test_compute_bearing_above_threshold_is_computed() -> None:
    origin = GeoPoint(latitude=40.0, longitude=-3.0)
    moved_north = GeoPoint(latitude=40.001, longitude=-3.0)  # ~111 m
    result = compute_bearing(
        previous_position=origin,
        current_position=moved_north,
        previous_bearing_degrees=None,
        min_displacement_meters=25.0,
    )
    assert result.source == BearingSource.COMPUTED
    assert result.bearing_degrees is not None
    assert math.isclose(result.bearing_degrees, 0.0, abs_tol=0.5)
