from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

import cercanias_worker.check_feed_freshness as module
from cercanias_connectors_renfe.freshness import FreshnessReport, FreshnessSample
from cercanias_connectors_renfe.gtfs_rt_client import GtfsRtFetchError


def _fake_report() -> FreshnessReport:
    s1 = FreshnessSample(
        now_utc=datetime(2026, 7, 31, 12, 0, 5, tzinfo=UTC),
        header_timestamp_raw="1785499200",
        header_timestamp_epoch_utc=datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC),
        delta_seconds=5.0,
    )
    s2 = FreshnessSample(
        now_utc=datetime(2026, 7, 31, 12, 0, 30, tzinfo=UTC),
        header_timestamp_raw="1785499220",
        header_timestamp_epoch_utc=datetime(2026, 7, 31, 12, 0, 20, tzinfo=UTC),
        delta_seconds=10.0,
    )
    return FreshnessReport(
        sample_1=s1,
        sample_2=s2,
        wall_clock_elapsed_seconds=25.0,
        header_timestamp_delta_seconds=20,
        feed_clock_skew_seconds=7.5,
    )


def test_main_writes_markdown_report_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))
    monkeypatch.setattr(module, "measure_feed_freshness", lambda *a, **k: _fake_report())

    exit_code = module.main()

    assert exit_code == 0
    report_path = tmp_path / "feed-freshness.md"
    assert report_path.exists()
    content = report_path.read_text(encoding="utf-8")
    assert "1785499200" in content
    assert "feed_clock_skew_seconds" in content


def test_main_handles_fetch_error_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path))

    def _raise(*args: object, **kwargs: object) -> FreshnessReport:
        raise GtfsRtFetchError("no hay red en este test")

    monkeypatch.setattr(module, "measure_feed_freshness", _raise)

    exit_code = module.main()

    assert exit_code == 1
    content = (tmp_path / "feed-freshness.md").read_text(encoding="utf-8")
    assert "ANOMALIA" in content
    assert "no hay red en este test" in content
