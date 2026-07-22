"""Tests for Markdown rendering and frontmatter safety."""

from __future__ import annotations

from granola_exporter.models import Note
from granola_exporter.render import render_note, render_transcript


def test_note_frontmatter_and_sections(note_payload):
    """The rendered note carries frontmatter, summary, attendees and folders."""
    note = Note.from_api(note_payload)
    md = render_note(note, has_transcript_file=True)

    assert md.startswith("---\n")
    assert 'id: "not_1d3tmYTlCICgjy"' in md
    assert "## Summary" in md
    assert "Approved the budget." in md
    assert "Oat Benson <oat@granola.ai>" in md
    assert "Projects, Clients" in md
    assert "[Full transcript](transcript.md)" in md


def test_title_with_colon_is_quoted(note_payload):
    """A colon in a title must not break the YAML frontmatter block."""
    note = Note.from_api(note_payload)
    md = render_note(note, has_transcript_file=False)
    assert 'title: "Quarterly yoghurt budget: review"' in md


def test_title_with_quotes_is_escaped(note_payload):
    """Embedded double quotes are escaped rather than terminating the scalar."""
    note = Note.from_api({**note_payload, "title": 'He said "hi"'})
    md = render_note(note, has_transcript_file=False)
    assert 'title: "He said \\"hi\\""' in md


def test_uuid_recovered_from_web_url(note_payload):
    """The internal UUID is recovered from web_url for enrichment joins."""
    note = Note.from_api(note_payload)
    assert note.uuid == "d290f1ee-6c54-4b01-90e6-d701748f0851"


def test_uuid_absent_when_no_url(note_payload):
    """A note without a UUID anywhere reports None rather than guessing."""
    note = Note.from_api({**note_payload, "web_url": "https://notes.granola.ai/d/abc"})
    assert note.uuid is None


def test_transcript_groups_consecutive_speakers(note_payload):
    """Consecutive lines from one speaker collapse into a single block."""
    note = Note.from_api(note_payload)
    md = render_transcript(note)

    assert md is not None
    assert md.count("Oat Benson") == 1, "consecutive utterances should merge"
    assert "Hello everyone. Let us begin." in md


def test_transcript_offsets_are_relative(note_payload):
    """Timestamps render as offsets from the first utterance."""
    note = Note.from_api(note_payload)
    md = render_transcript(note)

    assert "[00:00] Oat Benson" in md
    assert "[01:10] Speaker B" in md, "falls back to diarization label when unnamed"


def test_transcript_none_when_empty(note_payload):
    """A note with no transcript yields no transcript file."""
    note = Note.from_api({**note_payload, "transcript": []})
    assert render_transcript(note) is None


def test_blank_utterances_are_dropped(note_payload):
    """Empty transcript lines never produce an orphan speaker heading."""
    payload = {
        **note_payload,
        "transcript": [
            {"speaker": {"name": "A"}, "text": "   ", "start_time": None},
        ],
    }
    assert render_transcript(Note.from_api(payload)) is None


def test_malformed_payload_does_not_raise():
    """A payload with wrong-typed fields degrades instead of crashing."""
    note = Note.from_api(
        {
            "id": "not_x",
            "title": None,
            "owner": "not-an-object",
            "attendees": "nope",
            "folder_membership": None,
            "transcript": {"unexpected": "shape"},
            "created_at": "not-a-date",
        }
    )
    assert note.display_title == "Untitled"
    assert note.attendees == []
    assert note.transcript == []
    assert note.created_at is None
    assert render_note(note, has_transcript_file=False)
