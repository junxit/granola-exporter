"""Markdown rendering for archived notes and transcripts.

Rendering is intentionally lossless-adjacent: everything here is derived from
``raw.json``, which is archived verbatim alongside the Markdown. If a template
changes, re-rendering never requires another API call.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .models import Note, Utterance


def _yaml_scalar(value: Any) -> str:
    """Render a Python value as a YAML scalar.

    Args:
        value: A string, bool, ``None``, or other scalar.

    Returns:
        A YAML-safe scalar. Strings are always double-quoted and escaped, so
        colons, hashes and leading symbols in meeting titles cannot break the
        frontmatter block.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    text = text.replace("\n", " ").replace("\r", " ")
    return f'"{text}"'


def _yaml_block(fields: dict[str, Any]) -> str:
    """Render a mapping as a YAML frontmatter block.

    Args:
        fields: Ordered mapping of frontmatter keys to scalars or string lists.
            Empty lists and ``None`` values are omitted.

    Returns:
        The frontmatter block including its ``---`` delimiters.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None or value == [] or value == "":
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {_yaml_scalar(item)}" for item in value)
        else:
            lines.append(f"{key}: {_yaml_scalar(value)}")
    lines.append("---")
    return "\n".join(lines)


def _offset(start: datetime | None, origin: datetime | None) -> str:
    """Format an utterance timestamp as an offset from the meeting start.

    Args:
        start: The utterance start time.
        origin: The first utterance's start time.

    Returns:
        ``MM:SS`` or ``H:MM:SS`` for long meetings, or an empty string if
        either timestamp is missing or the delta is negative.
    """
    if start is None or origin is None:
        return ""
    delta: timedelta = start - origin
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return ""
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def _speaker_runs(
    transcript: list[Utterance],
) -> list[tuple[str, datetime | None, list[str]]]:
    """Group consecutive utterances by the same speaker.

    Granola emits one entry per utterance, which renders as an unreadable wall
    of one-line headings. Collapsing consecutive lines from the same speaker
    produces something that reads like a conversation.

    Args:
        transcript: Utterances in chronological order.

    Returns:
        Tuples of ``(speaker_label, run_start_time, texts)``.
    """
    runs: list[tuple[str, datetime | None, list[str]]] = []
    for utterance in transcript:
        text = utterance.text.strip()
        if not text:
            continue
        label = utterance.speaker.label()
        if runs and runs[-1][0] == label:
            runs[-1][2].append(text)
        else:
            runs.append((label, utterance.start_time, [text]))
    return runs


def render_note(note: Note, has_transcript_file: bool) -> str:
    """Render ``note.md`` for a meeting.

    Args:
        note: The parsed note.
        has_transcript_file: Whether a sibling ``transcript.md`` was written.

    Returns:
        The full Markdown document.
    """
    event = note.calendar_event
    frontmatter = _yaml_block(
        {
            "id": note.id,
            "uuid": note.uuid,
            "title": note.display_title,
            "created_at": note.created_at.isoformat() if note.created_at else None,
            "updated_at": note.updated_at.isoformat() if note.updated_at else None,
            "owner": note.owner.label() or None,
            "attendees": [a.label() for a in note.attendees if a.label()],
            "folders": [f.name for f in note.folder_membership if f.name],
            "calendar_event": event.event_title if event else None,
            "organiser": event.organiser if event else None,
            "scheduled_start": (
                event.scheduled_start_time.isoformat()
                if event and event.scheduled_start_time
                else None
            ),
            "scheduled_end": (
                event.scheduled_end_time.isoformat()
                if event and event.scheduled_end_time
                else None
            ),
            "web_url": note.web_url or None,
            "has_transcript": bool(note.transcript),
            "source": "granola-public-api",
        }
    )

    parts = [frontmatter, "", f"# {note.display_title}", ""]

    if note.created_at:
        parts += [f"*{note.created_at:%A, %d %B %Y at %H:%M %Z}*".rstrip(), ""]

    if note.attendees:
        parts += ["## Attendees", ""]
        parts += [f"- {a.label()}" for a in note.attendees if a.label()]
        parts.append("")

    summary = note.summary_markdown or note.summary_text
    if summary:
        parts += ["## Summary", "", summary.strip(), ""]

    if note.folder_membership:
        names = ", ".join(f.name for f in note.folder_membership if f.name)
        if names:
            parts += ["## Folders", "", names, ""]

    if has_transcript_file:
        count = len(note.transcript)
        parts += [
            "## Transcript",
            "",
            f"[Full transcript]({'transcript.md'}) — {count} utterances.",
            "",
        ]

    if note.web_url:
        parts += [f"[Open in Granola]({note.web_url})", ""]

    return "\n".join(parts).rstrip() + "\n"


def render_transcript(note: Note) -> str | None:
    """Render ``transcript.md`` for a meeting.

    Args:
        note: The parsed note.

    Returns:
        The Markdown transcript, or ``None`` when the note has no transcript
        content worth writing.
    """
    runs = _speaker_runs(note.transcript)
    if not runs:
        return None

    origin = next(
        (u.start_time for u in note.transcript if u.start_time is not None), None
    )

    frontmatter = _yaml_block(
        {
            "id": note.id,
            "title": note.display_title,
            "created_at": note.created_at.isoformat() if note.created_at else None,
            "utterances": str(len(note.transcript)),
            "source": "granola-public-api",
        }
    )

    parts = [frontmatter, "", f"# {note.display_title} — Transcript", ""]

    for label, start, texts in runs:
        stamp = _offset(start, origin)
        heading = f"**{label}**" if not stamp else f"**[{stamp}] {label}**"
        parts += [heading, "", " ".join(texts), ""]

    return "\n".join(parts).rstrip() + "\n"
