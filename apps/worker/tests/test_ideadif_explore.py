from __future__ import annotations

import json
from pathlib import Path

import pytest

import cercanias_worker.ideadif_explore as module
from cercanias_connectors_adif.ideadif_explorer import CapabilitiesResult, IdeadifExplorationResult


def test_main_writes_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    def _fake_explore(*, wms_url: str, wfs_url: str, **kwargs: object) -> IdeadifExplorationResult:
        return IdeadifExplorationResult(
            wms=CapabilitiesResult(
                service="WMS",
                url=wms_url,
                ok=True,
                status_code=200,
                reason="OK",
                version="1.3.0",
                layer_or_typenames=("TN.RailTransportNetwork.RailwayStationNode",),
            ),
            wfs=CapabilitiesResult(
                service="WFS",
                url=wfs_url,
                ok=True,
                status_code=200,
                reason="OK",
                version="2.0.0",
                layer_or_typenames=("Tramificacion:Dependencias",),
            ),
        )

    monkeypatch.setattr(module, "explore_ideadif_capabilities", _fake_explore)

    exit_code = module.main()
    assert exit_code == 0

    snapshot = json.loads((tmp_path / "docs" / "informes" / "ideadif-capacidades.json").read_text())
    assert snapshot["generic_inspire"]["wms"]["ok"] is True
    assert snapshot["tramificacion"]["wfs"]["ok"] is True
