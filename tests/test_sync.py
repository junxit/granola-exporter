"""Tests for the sync orchestration layer.

These drive ``sync_public_api`` end to end against a mocked transport, which is
coverage the logic never had while it lived inside ``cli.cmd_sync``.
"""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from granola_exporter.public_api import PublicAPIClient, RateLimiter
from granola_exporter.store import Archive, content_hash
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


@pytest.mark.parametrize("answered", ["../../../../etc/passwd", "not_2d3tmYTlCICgjy"])
def test_a_detail_payload_for_another_note_is_not_archived(
    tmp_path, note_payload, stub_payload, answered
):
    """The detail's id builds the path, so it must be the note asked for.

    Regression: a malformed id in the detail payload raised
    UnsafeArchivePathError and aborted the whole run, and a well-formed but
    different one filed this note's content under another note's key.
    """
    wrong = dict(note_payload, id=answered)
    counts = sync_public_api(Archive(tmp_path), _client(_handler(stub_payload, wrong, [])))

    assert (counts.failed, counts.new) == (1, 0)
    assert Archive(tmp_path).load_index() == {}


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


def test_index_survives_any_exception_midway(tmp_path, note_payload, stub_payload):
    """The save-before-raise promise covers every exception, not only API ones."""
    second = dict(stub_payload, id="not_2d3tmYTlCICgjy")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/notes"):
            return httpx.Response(
                200,
                json={"notes": [stub_payload, second], "hasMore": False, "cursor": None},
            )
        if request.url.path.endswith(second["id"]):
            raise RuntimeError("an unexpected failure")
        return httpx.Response(200, json=note_payload)

    archive = Archive(tmp_path)
    with pytest.raises(RuntimeError):
        sync_public_api(archive, _client(handle))

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


# -- transcripts Granola deletes ----------------------------------------------


def _serve(tmp_path, payload: dict, stub_keys) -> None:
    """Sync once against an API serving a single note.

    Args:
        tmp_path: The archive root.
        payload: The note the detail endpoint returns.
        stub_keys: The fields the list endpoint carries.
    """
    stub = {key: payload[key] for key in stub_keys}
    sync_public_api(Archive(tmp_path), _client(_handler(stub, payload, [])))


def _archived(tmp_path, note_id: str) -> tuple[dict, dict]:
    """Read a note's index entry and its archived ``raw.json``.

    Args:
        tmp_path: The archive root.
        note_id: The note.

    Returns:
        The entry and the decoded payload.
    """
    entry = Archive(tmp_path).load_index()[note_id]
    raw = json.loads((tmp_path / entry["path"] / "raw.json").read_text(encoding="utf-8"))
    return entry, raw


@pytest.mark.parametrize(
    "drop",
    [
        lambda payload: payload.update(transcript=None),
        lambda payload: payload.pop("transcript"),
        lambda payload: payload.update(transcript=[]),
    ],
    ids=["null", "omitted", "empty"],
)
def test_a_transcript_gone_upstream_is_kept(tmp_path, note_payload, stub_payload, drop):
    """Granola can delete a transcript and keep serving its note.

    Regression: the next fetch rewrote raw.json without it, taking the only
    copy with per-utterance timing and speakers; transcript.md survived, but
    note.md stopped linking it and the index said there was none.
    """
    _serve(tmp_path, note_payload, stub_payload)
    directory = tmp_path / Archive(tmp_path).load_index()[note_payload["id"]]["path"]
    transcript_md = (directory / "transcript.md").read_bytes()

    later = copy.deepcopy(note_payload)
    drop(later)
    later["updated_at"] = "2026-03-01T00:00:00Z"
    _serve(tmp_path, later, stub_payload)

    entry, raw = _archived(tmp_path, note_payload["id"])
    assert raw["transcript"] == note_payload["transcript"], "the verbatim copy survives"
    assert raw["updated_at"] == later["updated_at"], "the rest is still refreshed"
    assert (directory / "transcript.md").read_bytes() == transcript_md
    note_md = (directory / "note.md").read_text(encoding="utf-8")
    assert "[Full transcript](transcript.md)" in note_md
    assert entry["has_transcript"] is True
    assert entry["transcript_upstream_missing"] is True
    assert entry["content_hash"] == content_hash(raw), "the hash describes the disk"


def test_a_kept_transcript_stays_until_granola_serves_one(
    tmp_path, note_payload, stub_payload
):
    """Kept through later edits; a transcript Granola serves again wins."""
    _serve(tmp_path, note_payload, stub_payload)
    gone = dict(note_payload, transcript=None, updated_at="2026-03-01T00:00:00Z")
    _serve(tmp_path, gone, stub_payload)
    since = Archive(tmp_path).load_index()[note_payload["id"]]["transcript_missing_since"]

    # A rename moves the directory, so the kept copy is read from the old one.
    _serve(tmp_path, dict(gone, title="Renamed", updated_at="2026-04-01T00:00:00Z"), stub_payload)
    entry, raw = _archived(tmp_path, note_payload["id"])
    assert "renamed" in entry["path"]
    assert raw["transcript"] == note_payload["transcript"]
    assert entry["transcript_missing_since"] == since, "when it went, not when last seen"

    named = copy.deepcopy(note_payload["transcript"])
    named[2]["speaker"]["name"] = "Milk Jones"
    _serve(tmp_path, dict(gone, transcript=named, updated_at="2026-05-01T00:00:00Z"), stub_payload)
    entry, raw = _archived(tmp_path, note_payload["id"])
    assert raw["transcript"] == named
    assert "transcript_upstream_missing" not in entry
    assert "transcript_missing_since" not in entry


def test_a_transcript_is_kept_after_an_interrupted_move(
    tmp_path, note_payload, stub_payload
):
    """An interrupted relocation leaves the files where the note is going.

    The index still names the directory they left, so the kept transcript is
    looked for in both places rather than taken as absent.
    """
    _serve(tmp_path, note_payload, stub_payload)
    archive = Archive(tmp_path)
    index = json.loads(archive.index_path.read_text(encoding="utf-8"))
    index[note_payload["id"]]["path"] = "2026/01/moved-away"
    archive.index_path.write_text(json.dumps(index), encoding="utf-8")

    gone = dict(note_payload, transcript=None, updated_at="2026-03-01T00:00:00Z")
    _serve(tmp_path, gone, stub_payload)

    entry, raw = _archived(tmp_path, note_payload["id"])
    assert raw["transcript"] == note_payload["transcript"]
    assert entry["transcript_upstream_missing"] is True


# -- account identity -------------------------------------------------------


def test_account_email_reports_the_dominant_owner(stub_payload):
    """The public API exposes no account endpoint, so ownership stands in."""
    colleague = dict(stub_payload, id="not_colleagueAAAAA")
    colleague["owner"] = {"name": "Milk Jones", "email": "milk@granola.ai"}
    page = {"notes": [stub_payload, stub_payload, colleague], "hasMore": False}

    def handler(request):
        return httpx.Response(200, json=page)

    assert _client(handler).account_email() == "oat@granola.ai"


def test_account_email_is_empty_without_notes():
    """A key that can see nothing identifies nobody, and must not guess."""

    def handler(request):
        return httpx.Response(200, json={"notes": [], "hasMore": False})

    assert _client(handler).account_email() == ""
