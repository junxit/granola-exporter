"""Tests for archive layout, change detection and retention guarantees."""

from __future__ import annotations

from pathlib import Path

from granola_exporter.models import Note
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
