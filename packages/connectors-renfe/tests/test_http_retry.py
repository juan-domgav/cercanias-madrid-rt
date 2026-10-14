from __future__ import annotations

import httpx
import pytest

from cercanias_connectors_renfe.http_retry import get_with_retries


def _client(statuses: list[int | Exception]) -> tuple[httpx.Client, list[int]]:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        item = statuses[min(len(calls), len(statuses) - 1)]
        calls.append(1)
        if isinstance(item, Exception):
            raise item
        return httpx.Response(item, json={"ok": True})

    return httpx.Client(transport=httpx.MockTransport(handler)), calls


def test_retries_5xx_then_succeeds() -> None:
    client, calls = _client([503, 502, 200])
    waits: list[float] = []
    response = get_with_retries(client, "https://x/feed", sleep=waits.append)
    assert response.status_code == 200
    assert len(calls) == 3
    assert waits == [1.0, 2.0]


def test_retries_transport_error() -> None:
    client, calls = _client([httpx.ConnectError("caido"), 200])
    assert get_with_retries(client, "https://x/feed", sleep=lambda _s: None).status_code == 200
    assert len(calls) == 2


def test_gives_up_after_retries() -> None:
    client, calls = _client([500])
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retries(client, "https://x/feed", retries=2, sleep=lambda _s: None)
    assert len(calls) == 3


def test_does_not_retry_4xx() -> None:
    client, calls = _client([404])
    with pytest.raises(httpx.HTTPStatusError):
        get_with_retries(client, "https://x/feed", sleep=lambda _s: None)
    assert len(calls) == 1
