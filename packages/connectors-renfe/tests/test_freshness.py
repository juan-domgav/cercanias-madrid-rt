from __future__ import annotations

import httpx
import pytest

from cercanias_connectors_renfe import freshness as freshness_module
from cercanias_connectors_renfe.freshness import measure_feed_freshness


def test_measure_feed_freshness_computes_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    """No se duerme 25 s de verdad en el test: se parchea time.sleep. Los
    timestamps de header se controlan para verificar el calculo de deltas."""
    monkeypatch.setattr(freshness_module.time, "sleep", lambda _seconds: None)

    timestamps = iter(["1000000000", "1000000020"])
    real_client_class = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"header": {"timestamp": next(timestamps)}, "entity": []})

    def fake_client_factory(*args: object, **kwargs: object) -> httpx.Client:
        return real_client_class(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(freshness_module.httpx, "Client", fake_client_factory)

    report = measure_feed_freshness(wait_seconds=25.0)

    assert report.sample_1.header_timestamp_raw == "1000000000"
    assert report.sample_2.header_timestamp_raw == "1000000020"
    assert report.header_timestamp_delta_seconds == 20
    # El reloj real de la maquina de test esta muy por delante del epoch
    # 1000000000 (2001), asi que el delta debe ser un numero positivo grande,
    # no un desfase de segundos: valida solo la relacion, no un valor exacto.
    assert report.sample_1.delta_seconds > 0
    assert report.sample_2.delta_seconds > 0
    assert report.feed_clock_skew_seconds > 0
    assert report.wall_clock_elapsed_seconds >= 0


def test_measure_feed_freshness_propagates_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(freshness_module.time, "sleep", lambda _seconds: None)
    real_client_class = httpx.Client

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    def fake_client_factory(*args: object, **kwargs: object) -> httpx.Client:
        return real_client_class(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(freshness_module.httpx, "Client", fake_client_factory)

    with pytest.raises(Exception):  # noqa: B017 - GtfsRtFetchError
        measure_feed_freshness(wait_seconds=1.0)
