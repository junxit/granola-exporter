"""Tests for archive layout, change detection and retention guarantees."""

from __future__ import annotations

from pathlib import Path

from granola_exporter.models import SOURCE_MCP, SOURCE_PUBLIC_API, Note
from granola_exporter.render import render_note, render_transcript
from granola_exporter.store import Archive, content_hash, slugify


def _write(archive: Archive, payload: dict) -> Note:
    """Render and write a payload into the archive.

    Args:
        archive: Target archive.
        payload: A note payload.

    Returns:
        The parsed note that was written.
    """
    note = Note.from_api(payload)
    transcript_md = render_transcript(note)
    archive.write_note(
        note, render_note(note, bool(transcript_md)), transcript_md
    )
    return note


def test_slugify():
    """Titles reduce to safe, bounded slugs."""
    assert slugify("Quarterly yoghurt budget: review") == "quarterly-yoghurt-budget-review"
    assert slugify("  ///  ") == "untitled"
    assert len(slugify("x" * 200)) <= 60


def test_layout_is_year_month_day(tmp_path: Path, note_payload):
    """Notes are filed under YYYY/MM with a dated, slugged, id-suffixed dir."""
    archive = Archive(tmp_path)
    note = _write(archive, note_payload)
    path = archive.note_dir(note)

    assert path.parent.parent.name == "2026"
    assert path.parent.name == "01"
    assert path.name.startswith("2026-01-27--quarterly-yoghurt-budget-review")
    assert path.name.endswith("not_1d3tmYTlCICgjy")
    assert (path / "raw.json").is_file()
    assert (path / "note.md").is_file()
    assert (path / "transcript.md").is_file()


def test_undated_notes_are_filed_separately(tmp_path: Path, note_payload):
    """A note without a creation date still gets a stable home."""
    archive = Archive(tmp_path)
    note = _write(archive, {**note_payload, "created_at": None})
    assert archive.note_dir(note).parent.name == "undated"


def test_unchanged_detection(tmp_path: Path, note_payload):
    """An identical payload is recognised as unchanged."""
    archive = Archive(tmp_path)
    note = _write(archive, note_payload)
    assert archive.is_unchanged(note.id, content_hash(note.raw))
    assert not archive.is_unchanged(note.id, "different-hash")


def test_unchanged_is_false_if_directory_deleted(tmp_path: Path, note_payload):
    """A matching hash does not count if the files are gone from disk."""
    archive = Archive(tmp_path)
    note = _write(archive, note_payload)
    digest = content_hash(note.raw)

    import shutil

    shutil.rmtree(archive.note_dir(note))
    assert not archive.is_unchanged(note.id, digest)


def test_retitle_relocates_instead_of_duplicating(tmp_path: Path, note_payload):
    """Renaming a meeting moves its directory rather than leaving a stale copy."""
    archive = Archive(tmp_path)
    first = _write(archive, note_payload)
    old_path = archive.note_dir(first)
    assert old_path.is_dir()

    second = _write(archive, {**note_payload, "title": "Renamed meeting"})
    new_path = archive.note_dir(second)

    assert new_path.is_dir()
    assert not old_path.is_dir(), "the pre-rename directory should not survive"
    dirs = list((tmp_path / "2026" / "01").iterdir())
    assert len(dirs) == 1, "a rename must not duplicate the archive entry"


def test_upstream_missing_is_flagged_never_deleted(tmp_path: Path, note_payload):
    """A note removed upstream is retained on disk and flagged in the index."""
    archive = Archive(tmp_path)
    note = _write(archive, note_payload)
    path = archive.note_dir(note)

    newly_missing = archive.mark_upstream_missing(set())

    assert newly_missing == [note.id]
    assert archive.load_index()[note.id]["upstream_missing"] is True
    assert path.is_dir(), "archived content must survive upstream deletion"
    assert (path / "raw.json").is_file()


def test_reappearing_note_clears_the_flag(tmp_path: Path, note_payload):
    """A note that returns upstream has its missing flag cleared."""
    archive = Archive(tmp_path)
    note = _write(archive, note_payload)

    archive.mark_upstream_missing(set())
    archive.mark_upstream_missing({note.id})

    entry = archive.load_index()[note.id]
    assert entry["upstream_missing"] is False
    assert "missing_since" not in entry


def test_index_and_state_roundtrip(tmp_path: Path, note_payload):
    """Index and watermark survive being written and re-read."""
    archive = Archive(tmp_path)
    _write(archive, note_payload)
    archive.save_index()
    archive.save_state(updated_after="2026-01-27T16:45:00Z")

    reopened = Archive(tmp_path)
    assert "not_1d3tmYTlCICgjy" in reopened.load_index()
    assert reopened.watermark == "2026-01-27T16:45:00Z"


def test_corrupt_index_does_not_crash(tmp_path: Path):
    """A truncated index is treated as empty rather than aborting the sync."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "index.json").write_text("{not json", encoding="utf-8")
    assert Archive(tmp_path).load_index() == {}


# -- provenance and per-source state ---------------------------------------


def _mcp_note(payload: dict, uuid: str) -> Note:
    """Build a Note standing in for one archived through MCP.

    Args:
        payload: A note payload to base it on.
        uuid: The meeting UUID that joins the two backends.

    Returns:
        A degraded, MCP-keyed note.
    """
    note = Note.from_api(dict(payload, web_url=f"https://notes.granola.ai/d/{uuid}"))
    note.id = f"mcp_{uuid}"
    note.source = SOURCE_MCP
    note.degraded = True
    note.raw = dict(note.raw, uuid=uuid)
    return note


def test_write_note_records_provenance(tmp_path, note_payload):
    """Every entry carries its source; MCP entries are flagged degraded."""
    archive = Archive(tmp_path)
    _write(archive, note_payload)
    assert archive.load_index()[note_payload["id"]]["source"] == SOURCE_PUBLIC_API
    assert archive.archived_source(note_payload["id"]) == SOURCE_PUBLIC_API

    note = _mcp_note(note_payload, "e96c1c66-ed02-4a32-9acd-54720f8761b1")
    archive.write_note(note, "# x", None, source=SOURCE_MCP)
    entry = archive.load_index()[note.id]
    assert entry["source"] == SOURCE_MCP
    assert entry["degraded"] is True


def test_entries_without_a_source_read_as_public_api(tmp_path, note_payload):
    """Pre-provenance archives must not be misread as MCP-sourced."""
    archive = Archive(tmp_path)
    _write(archive, note_payload)
    index = archive.load_index()
    del index[note_payload["id"]]["source"]
    assert archive.archived_source(note_payload["id"]) == SOURCE_PUBLIC_API


def test_write_note_accepts_an_explicit_digest(tmp_path, note_payload):
    """The MCP backend hashes verbatim tool output, not the whole payload."""
    archive = Archive(tmp_path)
    note = Note.from_api(note_payload)
    archive.write_note(note, "# x", None, digest="deadbeef", extra={"mcp": {"v": 1}})
    entry = archive.load_index()[note.id]
    assert entry["content_hash"] == "deadbeef"
    assert entry["mcp"] == {"v": 1}
    assert archive.is_unchanged(note.id, "deadbeef")


def test_mark_upstream_missing_is_scoped_by_source(tmp_path, note_payload):
    """An MCP sweep must never flag public API notes as missing.

    Without the source filter this iterates the whole index, so a single MCP
    sync would flag every REST-archived note as gone upstream.
    """
    archive = Archive(tmp_path)
    _write(archive, note_payload)
    mcp = _mcp_note(note_payload, "e96c1c66-ed02-4a32-9acd-54720f8761b1")
    archive.write_note(mcp, "# x", None, source=SOURCE_MCP)

    flagged = archive.mark_upstream_missing(set(), source=SOURCE_MCP)

    assert flagged == [mcp.id]
    assert archive.load_index()[note_payload["id"]]["upstream_missing"] is False


def test_uuid_index_maps_uuids_to_keys(tmp_path, note_payload):
    """The uuid index is the join key across both backends."""
    archive = Archive(tmp_path)
    note = _write(archive, note_payload)
    mapping = archive.uuid_index()
    assert mapping[note.uuid] == [note.id]


def test_adopt_mcp_entry_relocates_instead_of_duplicating(tmp_path, note_payload):
    """A key arriving later upgrades the MCP note rather than duplicating it."""
    uuid = "d290f1ee-6c54-4b01-90e6-d701748f0851"  # the fixture's web_url uuid
    archive = Archive(tmp_path)
    mcp = _mcp_note(note_payload, uuid)
    archive.write_note(mcp, "# x", None, source=SOURCE_MCP)
    old_dir = archive.root / archive.load_index()[mcp.id]["path"]
    assert old_dir.is_dir()

    note = Note.from_api(note_payload)
    retired = archive.adopt_mcp_entry(note)
    _write(archive, note_payload)

    index = archive.load_index()
    assert retired == mcp.id
    assert mcp.id not in index, "the MCP key must be retired"
    assert note_payload["id"] in index
    assert not old_dir.exists(), "the old directory must be moved, not left behind"
    dupes = {u: k for u, k in archive.uuid_index().items() if len(k) > 1}
    assert not dupes, f"adoption left a duplicate: {dupes}"


def test_per_source_state_round_trips_and_mirrors(tmp_path):
    """Namespaced state persists, and the legacy key stays mirrored."""
    archive = Archive(tmp_path)
    archive.save_source_state(SOURCE_PUBLIC_API, updated_after="2026-01-27T16:45:00Z")
    archive.save_source_state(SOURCE_MCP, scanned_through="2026-08-06")

    reopened = Archive(tmp_path)
    assert reopened.watermark == "2026-01-27T16:45:00Z"
    assert reopened.source_state(SOURCE_MCP)["scanned_through"] == "2026-08-06"
    assert reopened.load_state()["updated_after"] == "2026-01-27T16:45:00Z", (
        "the legacy top-level key must stay mirrored for older builds"
    )


def test_legacy_flat_state_still_yields_a_watermark(tmp_path):
    """An archive written before per-source state keeps its watermark.

    Losing it would silently turn the next run into a full backfill.
    """
    archive = Archive(tmp_path)
    archive.state_path.parent.mkdir(parents=True, exist_ok=True)
    archive.state_path.write_text(
        '{"updated_after": "2026-07-29T01:53:06.032Z"}', encoding="utf-8"
    )
    assert Archive(tmp_path).watermark == "2026-07-29T01:53:06.032Z"
