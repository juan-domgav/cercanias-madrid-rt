from __future__ import annotations

import io
import zipfile
from pathlib import Path

import httpx
import pytest

from cercanias_connectors_renfe.gtfs_static_downloader import (
    GtfsStaticSource,
    GtfsStaticUnavailableError,
    download_gtfs_static,
    validate_zip_bytes,
)

MINIMAL_GTFS_FILES = {
    "agency.txt": "agency_id,agency_name,agency_url,agency_timezone\n1071VC,Renfe Cercanias,https://example.test,Europe/Madrid\n",
    "routes.txt": "route_id,route_short_name,route_long_name,route_type\n10T0001C1,C1,Test line,2\n",
    "trips.txt": "route_id,service_id,trip_id,trip_headsign\n10T0001C1,1080J,1080J20316C4a,\n",
    "stops.txt": "stop_id,stop_name,stop_lat,stop_lon\n18000,Madrid-Atocha,40.4066191,-3.6894391\n",
    "stop_times.txt": (
        "trip_id,arrival_time,departure_time,stop_id,stop_sequence\n"
        "1080J20316C4a,10:00:00,10:00:00,18000,1\n"
    ),
    "calendar.txt": (
        "service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date\n"
        "1080J,1,1,1,1,1,0,0,20260101,20261231\n"
    ),
}


def _build_zip_bytes(files: dict[str, str] | None = None) -> bytes:
    files = files if files is not None else MINIMAL_GTFS_FILES
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    return buffer.getvalue()


def test_validate_zip_bytes_valid_minimal_gtfs() -> None:
    result = validate_zip_bytes(_build_zip_bytes())
    assert result.valid
    assert result.errors == ()


def test_validate_zip_bytes_missing_required_file() -> None:
    files = dict(MINIMAL_GTFS_FILES)
    del files["stops.txt"]
    result = validate_zip_bytes(_build_zip_bytes(files))
    assert not result.valid
    assert any("stops.txt" in e for e in result.errors)


def test_validate_zip_bytes_missing_required_column() -> None:
    files = dict(MINIMAL_GTFS_FILES)
    files["stops.txt"] = "stop_id,stop_name\n18000,Madrid-Atocha\n"
    result = validate_zip_bytes(_build_zip_bytes(files))
    assert not result.valid
    assert any("stop_lat" in e for e in result.errors)


def test_validate_zip_bytes_not_a_zip() -> None:
    result = validate_zip_bytes(b"<html>not a zip</html>")
    assert not result.valid


def test_download_cascade_falls_back_to_third_source(tmp_path: Path) -> None:
    """Simula el escenario real observado en Fase 0: las dos primeras
    fuentes devuelven HTML (no ZIP), la tercera sirve un ZIP valido."""

    def handler(request: httpx.Request) -> httpx.Response:
        if "nap.transportes" in str(request.url):
            return httpx.Response(200, html="<html>login required</html>")
        if "datos.crtm.es" in str(request.url):
            return httpx.Response(200, html="<html>arcgis hub spa</html>")
        if "ssl.renfe.com" in str(request.url):
            return httpx.Response(
                200,
                content=_build_zip_bytes(),
                headers={"etag": '"abc123"', "content-type": "application/zip"},
            )
        raise AssertionError(f"URL inesperada: {request.url}")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = download_gtfs_static(tmp_path, client=client)

    assert outcome.source_used.source_name == "ssl.renfe.com"
    assert outcome.changed is True
    assert outcome.validation.valid
    assert len(outcome.attempts) == 3
    assert outcome.attempts[0].ok is False
    assert outcome.attempts[1].ok is False
    assert outcome.attempts[2].ok is True
    assert (outcome.version_dir / "stops.txt").exists()


def test_download_all_sources_fail_raises_explicit_error(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(GtfsStaticUnavailableError):
        download_gtfs_static(tmp_path, client=client)


def test_download_detects_no_change_via_etag(tmp_path: Path) -> None:
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "ssl.renfe.com" in str(request.url):
            call_count["n"] += 1
            return httpx.Response(
                200, content=_build_zip_bytes(), headers={"etag": '"same-etag"'}
            )
        return httpx.Response(200, html="<html>no</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = download_gtfs_static(tmp_path, client=client)
    assert first.changed is True

    second = download_gtfs_static(tmp_path, client=client)
    assert second.changed is False
    assert second.version_dir == first.version_dir


def test_download_detects_change_via_new_etag(tmp_path: Path) -> None:
    etag_holder = {"value": '"etag-v1"'}

    def handler(request: httpx.Request) -> httpx.Response:
        if "ssl.renfe.com" in str(request.url):
            return httpx.Response(
                200, content=_build_zip_bytes(), headers={"etag": etag_holder["value"]}
            )
        return httpx.Response(200, html="<html>no</html>")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = download_gtfs_static(tmp_path, client=client)

    etag_holder["value"] = '"etag-v2"'
    second = download_gtfs_static(tmp_path, client=client)

    assert second.changed is True
    assert second.version_dir != first.version_dir


def test_custom_sources_override_default_cascade(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_build_zip_bytes(), headers={"etag": '"x"'})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    only_source = (GtfsStaticSource(name="custom", url="https://example.test/custom.zip"),)
    outcome = download_gtfs_static(tmp_path, sources=only_source, client=client)
    assert outcome.source_used.source_name == "custom"
