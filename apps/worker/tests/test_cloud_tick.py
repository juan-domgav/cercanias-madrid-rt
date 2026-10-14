from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from cercanias_storage.publishers import LocalSnapshotPublisher
from cercanias_worker import cloud_tick


def _snapshot(service: str) -> dict[str, Any]:
    return {"vehicles": [{"trip_id": f"{service}12345C5", "delay_seconds": 0}]}


def _write(base: Path, key: str, payload: object | str) -> None:
    target = base / key
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload if isinstance(payload, str) else json.dumps(payload))


def test_missing_schedule_sets_health_and_logs_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    publisher = LocalSnapshotPublisher(tmp_path)
    boards = cloud_tick._build_boards(_snapshot("1085X"), publisher)

    assert boards["health"]["schedule_missing"] is True
    assert boards["health"]["no_departures"] is True
    assert boards["health"]["schedule_days_left"] == 0
    entry = json.loads(capsys.readouterr().err.strip().splitlines()[-1])
    assert entry["severity"] == "ERROR"
    assert entry["alert"] == "schedule_missing"
    assert entry["service"] == "1085X"


def test_corrupt_schedule_does_not_raise(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "horarios/1085X.json", "{no es json")
    publisher = LocalSnapshotPublisher(tmp_path)

    assert cloud_tick._load_day_schedule(publisher, "1085X") == {}
    assert '"severity": "ERROR"' in capsys.readouterr().err


def test_days_left_counts_consecutive_days_with_wraparound(tmp_path: Path) -> None:
    for service in ("1097L", "1098M", "1099X", "1001J", "1002V", "1005L"):
        _write(tmp_path, f"horarios/{service}.json", {"service": service, "stations": {}})
    publisher = LocalSnapshotPublisher(tmp_path)

    # 1098M, 1099X, 1001J y 1002V siguen sin hueco; 1005L queda tras el hueco.
    assert cloud_tick._schedule_days_left(publisher, "1097L") == 4


def test_days_left_is_none_without_listing() -> None:
    class SinListado:
        def read(self, key: str) -> bytes | None:
            return None

    assert cloud_tick._schedule_days_left(SinListado(), "1085X") is None


def test_safe_publish_swallows_storage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    class Roto:
        def publish(self, *_args: object, **_kwargs: object) -> str:
            raise RuntimeError("503 GCS")

    assert cloud_tick._safe_publish(Roto(), "boards.json", b"{}", content_type="application/json") is None
    entry = json.loads(capsys.readouterr().err.strip())
    assert entry["severity"] == "ERROR"
    assert entry["key"] == "boards.json"
