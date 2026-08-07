"""Tests for the sync orchestration layer.

These drive ``sync_public_api`` end to end against a mocked transport, which is
coverage the logic never had while it lived inside ``cli.cmd_sync``.
"""

from __future__ import annotations

import httpx
import pytest

from granola_exporter.public_api import PublicAPIClient, RateLimiter
from granola_exporter.store import Archive
from granola_exporter.sync import SyncOptions, sync_public_api

BASE = "https://api.test/v1"


def _client(handler) -> PublicAPIClient:
    """Build a client whose transport is a mock, with pacing disabled.

    Args:
        handler: A callable taking an ``httpx.Request`` and returning a
            response.

    Returns:
        A client wired to the mock transport.
    """
    client = PublicAPIClient("grn_test", base_url=BASE)
    client._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer grn_test"},
    )
    client._limiter = RateLimiter(capacity=1000, rate=1e6)
    return client


def _handler(stub: dict, note: dict, calls: list[str]):
    """Serve one note through the list and detail endpoints.

    Args:
        stub: The note stub returned by the list endpoint.
        note: The full note returned by the detail endpoint.
        calls: A log every request path is appended to.

    Returns:
        A request handler for ``httpx.MockTransport``.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/notes"):
            return httpx.Response(
                200, json={"notes": [stub], "hasMore": False, "cursor": None}
            )
        return httpx.Response(200, json=note)

    return handle


def test_backfill_writes_the_note(tmp_path, note_payload, stub_payload):
    """A first run archives the note and records a watermark."""
    archive = Archive(tmp_path)
    calls: list[str] = []
    counts = sync_public_api(
        archive, _client(_handler(stub_payload, note_payload, calls))
    )

    assert counts.new == 1
    assert counts.detail_fetches == 1
    assert archive.watermark == note_payload["updated_at"]

    entry = archive.load_index()[note_payload["id"]]
    assert (archive.root / entry["path"] / "note.md").is_file()
    assert (archive.root / entry["path"] / "raw.json").is_file()


def test_resync_costs_zero_detail_fetches(tmp_path, note_payload, stub_payload):
    """The stub's unchanged ``updated_at`` skips the detail call entirely.

    Regression test. The index stores ``datetime.isoformat`` output
    (``2026-01-27T16:45:00+00:00``) while the API sends ``...00Z``, so the
    original string comparison never matched and every note fell through to a
    detail fetch. The skip only works when the two are compared as instants.
    """
    archive = Archive(tmp_path)
    calls: list[str] = []
    handler = _handler(stub_payload, note_payload, calls)
    sync_public_api(archive, _client(handler))

    calls.clear()
    counts = sync_public_api(Archive(tmp_path), _client(handler))

    assert counts.unchanged == 1
    assert counts.detail_fetches == 0
    assert not [c for c in calls if c.endswith(note_payload["id"])]


def test_malformed_id_is_skipped_not_archived(tmp_path, note_payload):
    """An id that could build a path outside the archive is refused."""
    archive = Archive(tmp_path)
    evil = dict(note_payload, id="../../../../etc/passwd")
    counts = sync_public_api(archive, _client(_handler(evil, evil, [])))

    assert counts.skipped == 1
    assert counts.new == 0
    assert archive.load_index() == {}


def test_note_still_processing_is_skipped_not_failed(
    tmp_path, note_payload, stub_payload
):
    """A 404 on detail means "no summary yet" and must not fail the run."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/notes"):
            return httpx.Response(
                200, json={"notes": [stub_payload], "hasMore": False, "cursor": None}
            )
        return httpx.Response(404, json={"error": "not found"})

    archive = Archive(tmp_path)
    counts = sync_public_api(archive, _client(handle))

    assert counts.skipped == 1
    assert counts.failed == 0


def test_watermark_is_not_advanced_when_a_note_failed(
    tmp_path, note_payload, stub_payload, monkeypatch
):
    """A failed note must leave the watermark alone so the next run retries."""
    monkeypatch.setattr("granola_exporter.public_api.time.sleep", lambda _: None)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/notes"):
            return httpx.Response(
                200, json={"notes": [stub_payload], "hasMore": False, "cursor": None}
            )
        return httpx.Response(500, text="boom")

    archive = Archive(tmp_path)
    counts = sync_public_api(archive, _client(handle))

    assert counts.failed == 1
    assert archive.watermark is None


def test_index_survives_an_api_error_midway(tmp_path, note_payload, stub_payload):
    """Partial progress is saved before the error propagates."""
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/notes") and len(seen) == 1:
            return httpx.Response(
                200, json={"notes": [stub_payload], "hasMore": True, "cursor": "c1"}
            )
        if request.url.path.endswith("/notes"):
            return httpx.Response(401, json={"error": "nope"})
        return httpx.Response(200, json=note_payload)

    archive = Archive(tmp_path)
    with pytest.raises(Exception):
        sync_public_api(archive, _client(handle))

    assert archive.index_path.is_file(), "index must be saved before re-raising"
    assert note_payload["id"] in Archive(tmp_path).load_index()


def test_full_run_flags_notes_missing_upstream(tmp_path, note_payload, stub_payload):
    """A note that disappears upstream is flagged, never deleted."""
    archive = Archive(tmp_path)
    sync_public_api(archive, _client(_handler(stub_payload, note_payload, [])))
    entry_path = archive.root / archive.load_index()[note_payload["id"]]["path"]

    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"notes": [], "hasMore": False, "cursor": None})

    archive2 = Archive(tmp_path)
    sync_public_api(archive2, _client(empty), SyncOptions(full=True))

    assert archive2.load_index()[note_payload["id"]]["upstream_missing"] is True
    assert entry_path.is_dir(), "the archive must never delete a note"
