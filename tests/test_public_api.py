"""Tests for pagination, rate limiting and error handling."""

from __future__ import annotations

import httpx
import pytest

from granola_exporter.public_api import (
    GranolaAPIError,
    NoteNotFoundError,
    PublicAPIClient,
    RateLimiter,
)

BASE = "https://public-api.test/v1"


def _client(handler) -> PublicAPIClient:
    """Build a client whose transport is driven by ``handler``.

    Args:
        handler: A callable taking an ``httpx.Request`` and returning a
            ``httpx.Response``.

    Returns:
        A client wired to the mock transport.
    """
    client = PublicAPIClient("grn_test", base_url=BASE)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer grn_test"},
    )
    # Keep tests fast: the real limiter would pace these requests.
    client._limiter = RateLimiter(capacity=1000, rate=1e6)
    return client


def test_iter_notes_follows_cursors(stub_payload):
    """iter_notes walks every page until hasMore is false."""
    pages = [
        {"notes": [stub_payload], "hasMore": True, "cursor": "c1"},
        {"notes": [{**stub_payload, "id": "not_2"}], "hasMore": True, "cursor": "c2"},
        {"notes": [{**stub_payload, "id": "not_3"}], "hasMore": False, "cursor": None},
    ]
    seen_cursors = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cursors.append(request.url.params.get("cursor"))
        return httpx.Response(200, json=pages[len(seen_cursors) - 1])

    with _client(handler) as client:
        ids = [n["id"] for n in client.iter_notes()]

    assert ids == ["not_1d3tmYTlCICgjy", "not_2", "not_3"]
    assert seen_cursors == [None, "c1", "c2"]


def test_iter_notes_aborts_on_repeated_cursor(stub_payload):
    """A server that repeats a cursor must not spin forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"notes": [stub_payload], "hasMore": True, "cursor": "same"}
        )

    with _client(handler) as client:
        with pytest.raises(GranolaAPIError, match="cursor repeated"):
            list(client.iter_notes())


def test_retries_on_429_then_succeeds(stub_payload, monkeypatch):
    """A 429 is retried, honouring Retry-After, rather than failing the sync."""
    monkeypatch.setattr("granola_exporter.public_api.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"notes": [stub_payload], "hasMore": False})

    with _client(handler) as client:
        assert len(list(client.iter_notes())) == 1
    assert calls["n"] == 2


def test_404_raises_note_not_found():
    """Notes still processing surface as NoteNotFoundError, not a hard failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    with _client(handler) as client:
        with pytest.raises(NoteNotFoundError):
            client.get_note("not_mi55ingNote01x")


def test_401_is_fatal():
    """A bad key fails loudly rather than silently producing an empty archive."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    with _client(handler) as client:
        with pytest.raises(GranolaAPIError, match="401"):
            client.get_note("not_1d3tmYTlCICgjy")


def test_get_note_requests_transcript():
    """include=transcript is sent when the transcript is wanted."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["include"] = request.url.params.get("include")
        return httpx.Response(200, json={"id": "not_1d3tmYTlCICgjy"})

    with _client(handler) as client:
        client.get_note("not_1d3tmYTlCICgjy", include_transcript=True)
    assert captured["include"] == "transcript"


def test_page_size_is_clamped():
    """page_size never exceeds the documented maximum of 30."""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["page_size"] = request.url.params.get("page_size")
        return httpx.Response(200, json={"notes": [], "hasMore": False})

    with _client(handler) as client:
        client.list_notes_page(page_size=500)
    assert captured["page_size"] == "30"


def test_missing_api_key_raises():
    """Constructing a client without a key is an immediate, clear error."""
    with pytest.raises(GranolaAPIError, match="No API key"):
        PublicAPIClient("")


def test_rate_limiter_paces_beyond_burst(monkeypatch):
    """Once the burst is spent, the limiter sleeps instead of hammering."""
    slept = []
    monkeypatch.setattr(
        "granola_exporter.ratelimit.time.sleep", lambda s: slept.append(s)
    )
    limiter = RateLimiter(capacity=2, rate=5.0)
    for _ in range(2):
        limiter.acquire()
    assert slept == []
    limiter.acquire()
    assert slept, "expected the limiter to sleep once the burst was exhausted"
