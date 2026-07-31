from __future__ import annotations

from datetime import UTC, datetime

from cercanias_domain.enums import BearingSource, ResolutionMethod, VehicleStatus
from cercanias_domain.models import VehicleObservation
from cercanias_persistence.in_memory_state_repository import InMemoryCurrentStateRepository


def _obs(
    vehicle_id: str, lat: float, lon: float, bearing: float | None = None
) -> VehicleObservation:
    return VehicleObservation(
        entity_id=f"VP_{vehicle_id}",
        vehicle_id=vehicle_id,
        trip_id=f"1080J{vehicle_id}C1",
        nucleo_code="1080",
        nucleo_prefix="10",
        is_special_trip=False,
        route_raw="C1",
        route_group="C1",
        route_id=None,
        direction_id=None,
        trip_headsign=None,
        resolution=ResolutionMethod.FALLBACK_LABEL,
        has_position=True,
        latitude=lat,
        longitude=lon,
        current_status=VehicleStatus.IN_TRANSIT_TO,
        stop_id_raw="17000",
        stop_id_resolvable="17000",
        platform=None,
        observed_at=datetime.now(tz=UTC),
        bearing_degrees=bearing,
        bearing_source=BearingSource.NO_PREVIOUS_SNAPSHOT,
    )


def test_no_previous_state_for_unknown_vehicle() -> None:
    repo = InMemoryCurrentStateRepository()
    assert repo.get_previous_position("unknown") is None
    assert repo.get_previous_bearing("unknown") is None


def test_update_then_previous_reflects_prior_observation() -> None:
    repo = InMemoryCurrentStateRepository()
    first = _obs("20316", 40.35, -3.68, bearing=None)
    repo.update(first)

    # antes de la segunda actualizacion, "previous" debe ser la primera
    prev_pos = repo.get_previous_position("20316")
    assert prev_pos is not None
    assert prev_pos.latitude == 40.35

    second = _obs("20316", 40.36, -3.68, bearing=15.0)
    repo.update(second)

    prev_pos_after = repo.get_previous_position("20316")
    assert prev_pos_after is not None
    assert prev_pos_after.latitude == 40.36
    assert repo.get_previous_bearing("20316") == 15.0


def test_snapshot_size_tracks_distinct_vehicles() -> None:
    repo = InMemoryCurrentStateRepository()
    repo.update(_obs("1", 40.0, -3.0))
    repo.update(_obs("2", 40.1, -3.1))
    repo.update(_obs("1", 40.01, -3.0))
    assert repo.snapshot_size() == 2


def test_observation_without_position_has_no_previous_position() -> None:
    repo = InMemoryCurrentStateRepository()
    obs = _obs("30000", 40.0, -3.0).model_copy(
        update={"has_position": False, "latitude": None, "longitude": None}
    )
    repo.update(obs)
    assert repo.get_previous_position("30000") is None


def test_gps_gap_does_not_lose_last_known_position() -> None:
    """C-4: un frame sin `position` (Anexo A.7) no debe romper la cadena de rumbo.

    Regresion: `previous` se guardaba pero nunca se leia, asi que el primer
    frame sin GPS borraba de facto la ultima posicion conocida y el siguiente
    frame con GPS reiniciaba el rumbo a NO_PREVIOUS_SNAPSHOT.
    """
    repo = InMemoryCurrentStateRepository()
    repo.update(_obs("40000", 40.35, -3.68, bearing=42.0))

    sin_gps = _obs("40000", 0.0, 0.0).model_copy(
        update={"has_position": False, "latitude": None, "longitude": None}
    )
    repo.update(sin_gps)

    # `current` no trae GPS, pero `previous` si: debe recuperarse de ahi.
    prev = repo.get_previous_position("40000")
    assert prev is not None, "la posicion previa se perdio al llegar un frame sin GPS"
    assert prev.latitude == 40.35
    assert prev.longitude == -3.68


def test_gps_gap_does_not_lose_last_known_bearing() -> None:
    """C-4: por debajo del umbral (o sin datos) se conserva el ultimo rumbo conocido."""
    repo = InMemoryCurrentStateRepository()
    repo.update(_obs("40001", 40.35, -3.68, bearing=137.5))

    sin_gps = _obs("40001", 0.0, 0.0).model_copy(
        update={
            "has_position": False,
            "latitude": None,
            "longitude": None,
            "bearing_degrees": None,
        }
    )
    repo.update(sin_gps)

    assert repo.get_previous_bearing("40001") == 137.5
