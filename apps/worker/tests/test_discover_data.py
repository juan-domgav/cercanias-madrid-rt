from __future__ import annotations

import json
from pathlib import Path

import pytest

import cercanias_worker.discover_data as module
from cercanias_connectors_adif.station_correlation import (
    AdifDependencia,
    AdifDependenciasUnavailableError,
)
from cercanias_connectors_renfe.gtfs_rt_client import GtfsRtFetchError
from cercanias_connectors_renfe.gtfs_static_downloader import (
    DownloadOutcome,
    GtfsStaticUnavailableError,
    SourceAttempt,
    ZipValidationResult,
)
from cercanias_connectors_renfe.gtfs_static_index import CsvStaticScheduleIndex
from cercanias_domain.models import TripStaticInfo

REPO_FIXTURES_DIR = Path(__file__).resolve().parents[3] / "fixtures"


def test_main_degrades_gracefully_when_all_external_sources_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Puerta 0-B no debe bloquear nada, y sin GTFS estatico el informe debe
    seguir generandose marcando las anomalias, no reventar."""
    monkeypatch.setenv("GTFS_STATIC_DIR", str(tmp_path / "gtfs_static"))
    monkeypatch.setenv("MAPPINGS_DIR", str(tmp_path / "mappings"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "informes"))
    monkeypatch.setenv("FIXTURES_DIR", str(REPO_FIXTURES_DIR))
    monkeypatch.setenv("RENFE_NUCLEO_CODES", "1080")

    def _raise_static(*args: object, **kwargs: object) -> None:
        raise GtfsStaticUnavailableError([])

    def _raise_feed(*args: object, **kwargs: object) -> None:
        raise GtfsRtFetchError("sin red en el test")

    def _raise_adif(*args: object, **kwargs: object) -> None:
        raise AdifDependenciasUnavailableError("sin red en el test")

    monkeypatch.setattr(module, "download_gtfs_static", _raise_static)
    monkeypatch.setattr(module, "fetch_vehicle_positions_payload", _raise_feed)
    monkeypatch.setattr(module, "fetch_adif_dependencias", _raise_adif)

    exit_code = module.main()

    assert exit_code == 0
    estaciones = json.loads((tmp_path / "mappings" / "estaciones-madrid.json").read_text())
    assert estaciones["gtfs_source_used"] is None
    assert estaciones["total_stations"] == 11  # 13 entidades, 11 stop_id distintos en el fixture

    nucleos_content = (tmp_path / "mappings" / "nucleos.yaml").read_text(encoding="utf-8")
    assert "Madrid" in nucleos_content

    informe = (tmp_path / "informes" / "cobertura-estaciones.md").read_text(encoding="utf-8")
    assert "ANOMALIA" in informe
    assert "fixture" in informe
    assert "C9" in informe


def test_main_reports_full_resolution_with_working_static_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTFS_STATIC_DIR", str(tmp_path / "gtfs_static"))
    monkeypatch.setenv("MAPPINGS_DIR", str(tmp_path / "mappings"))
    monkeypatch.setenv("REPORTS_DIR", str(tmp_path / "informes"))
    monkeypatch.setenv("FIXTURES_DIR", str(REPO_FIXTURES_DIR))
    monkeypatch.setenv("RENFE_NUCLEO_CODES", "1080")

    fixture_payload = json.loads(
        (REPO_FIXTURES_DIR / "vehicle_positions_madrid.json").read_text(encoding="utf-8")
    )
    fake_index = CsvStaticScheduleIndex(
        trips={
            e["vehicle"]["trip"]["tripId"]: TripStaticInfo(
                route_id=f"10T{i:04d}",
                route_short_name=e["vehicle"]["trip"]["tripId"][-3:],
                direction_id=0,
                trip_headsign=None,
            )
            for i, e in enumerate(fixture_payload["entity"])
        },
        stops={},
    )

    def _fake_download(*args: object, **kwargs: object) -> DownloadOutcome:
        attempt = SourceAttempt(
            source_name="ssl.renfe.com",
            url="https://example.test",
            ok=True,
            status_code=200,
            reason="OK",
        )
        return DownloadOutcome(
            source_used=attempt,
            attempts=[attempt],
            version_dir=tmp_path,
            changed=True,
            validation=ZipValidationResult(valid=True, bad_file_in_zip=None, files=(), namelist=()),
        )

    monkeypatch.setattr(module, "download_gtfs_static", _fake_download)
    monkeypatch.setattr(module, "build_static_schedule_index", lambda _dir: fake_index)
    monkeypatch.setattr(
        module, "fetch_vehicle_positions_payload", lambda *a, **k: fixture_payload
    )
    monkeypatch.setattr(
        module,
        "fetch_adif_dependencias",
        lambda *a, **k: [
            AdifDependencia(nombre="Madrid-Atocha Cercanias", cod_depend="18000", cod_tipo_d="E"),
            AdifDependencia(nombre="Madrid-Chamartin", cod_depend="17000", cod_tipo_d="E"),
        ],
    )

    exit_code = module.main()
    assert exit_code == 0

    informe = (tmp_path / "informes" / "cobertura-estaciones.md").read_text(encoding="utf-8")
    assert "100.0%" in informe  # resolucion de trip_id via el indice estatico falso
