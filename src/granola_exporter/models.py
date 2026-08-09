"""Dataclasses mirroring the Granola public API schema.

These models exist for rendering convenience only. The verbatim API payload is
always written to ``raw.json`` first, so these parsers are deliberately
forgiving: an unexpected or missing field degrades the rendered Markdown but
never loses data and never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

NOTE_ID_RE = re.compile(r"^not_[a-zA-Z0-9]{14}$")
FOLDER_ID_RE = re.compile(r"^fol_[a-zA-Z0-9]{14}$")

# The internal API and the Granola MCP identify meetings by UUID, while the
# public API uses opaque `not_*` ids. Used to recover a UUID from `web_url` so
# the two namespaces can be joined for enrichment.
UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

# Archive key for a note that only the MCP backend can see. Deliberately
# stricter than NOTE_ID_RE: lowercase hex only, hyphens in exactly four fixed
# positions, fully anchored. Nothing matching it can contain "/", "\", ".."
# or a drive letter, so it is as safe to interpolate into a path as `not_*`.
MCP_NOTE_ID_RE = re.compile(
    r"^mcp_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

MCP_KEY_PREFIX = "mcp_"

SOURCE_PUBLIC_API = "granola-public-api"
SOURCE_MCP = "granola-mcp"


def is_valid_note_id(value: Any) -> bool:
    """Check that a value is a well-formed public API note id.

    Note ids are used to build filesystem paths and request URLs, so they are
    validated before use rather than trusted. Anything not matching the exact
    ``not_`` + 14 alphanumerics shape -- including any value containing a path
    separator or ``..`` -- is rejected.

    Args:
        value: A candidate id from an API payload.

    Returns:
        ``True`` if the value is a well-formed note id.
    """
    return isinstance(value, str) and NOTE_ID_RE.fullmatch(value) is not None


def is_valid_folder_id(value: Any) -> bool:
    """Check that a value is a well-formed public API folder id.

    Args:
        value: A candidate id from an API payload.

    Returns:
        ``True`` if the value is a well-formed folder id.
    """
    return isinstance(value, str) and FOLDER_ID_RE.fullmatch(value) is not None


def is_valid_uuid(value: Any) -> bool:
    """Check that a value is a canonical lowercase UUID.

    Applied to MCP meeting ids before they enter a request body or become an
    archive key, mirroring how ``is_valid_note_id`` guards the public API.

    Args:
        value: A candidate meeting id from an MCP response.

    Returns:
        ``True`` if the value is a well-formed UUID.
    """
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def is_valid_mcp_note_id(value: Any) -> bool:
    """Check that a value is a well-formed MCP archive key.

    Args:
        value: A candidate archive key.

    Returns:
        ``True`` if the value matches ``mcp_`` plus a canonical UUID.
    """
    return isinstance(value, str) and MCP_NOTE_ID_RE.fullmatch(value) is not None


def is_valid_archive_key(value: Any) -> bool:
    """Check that a value is an id the archive may turn into a path.

    This is the *only* validator that admits both namespaces. Transport-layer
    validators stay narrow on purpose: ``public_api.get_note`` must keep
    accepting ``not_*`` alone, because widening the archive-path check must
    never widen what can be interpolated into a request URL.

    Args:
        value: A candidate archive key.

    Returns:
        ``True`` if the value is a well-formed ``not_*`` or ``mcp_*`` key.
    """
    return is_valid_note_id(value) or is_valid_mcp_note_id(value)


def mcp_archive_key(meeting_id: Any) -> str | None:
    """Turn an MCP meeting UUID into an archive key.

    Args:
        meeting_id: A meeting id from an MCP response.

    Returns:
        The ``mcp_<uuid>`` key, or ``None`` when the id is not a valid UUID.
        Callers treat ``None`` as "skip this meeting" rather than sanitizing.
    """
    if not isinstance(meeting_id, str):
        return None
    candidate = meeting_id.strip().lower()
    if not is_valid_uuid(candidate):
        return None
    return f"{MCP_KEY_PREFIX}{candidate}"


def _text(value: Any) -> str:
    """Coerce an arbitrary API value to a stripped string.

    Args:
        value: Any value from a decoded JSON payload.

    Returns:
        The value as a stripped string, or an empty string if it is ``None``.
    """
    return "" if value is None else str(value).strip()


def parse_timestamp(value: Any) -> datetime | None:
    """Parse an ISO 8601 timestamp as returned by the Granola API.

    Also used to compare timestamps that have made a round trip through the
    index, where ``datetime.isoformat`` renders UTC as ``+00:00`` rather than
    the ``Z`` the API sends. Comparing the two as strings silently never
    matches, so callers compare the parsed instants instead.

    Args:
        value: An ISO 8601 string, possibly ``Z``-suffixed, or ``None``.

    Returns:
        The parsed datetime, or ``None`` if the value is absent or unparseable.
    """
    raw = _text(value)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass(slots=True)
class User:
    """A note owner or meeting attendee."""

    name: str = ""
    email: str = ""

    @classmethod
    def from_api(cls, data: Any) -> User:
        """Build a user from an API ``User`` object.

        Args:
            data: The decoded ``owner`` or ``attendees[]`` entry.

        Returns:
            The parsed user; empty fields where absent.
        """
        if not isinstance(data, dict):
            return cls()
        return cls(name=_text(data.get("name")), email=_text(data.get("email")))

    def label(self) -> str:
        """Render the user as ``Name <email>``, degrading gracefully.

        Returns:
            The best available human-readable label for this user.
        """
        if self.name and self.email:
            return f"{self.name} <{self.email}>"
        return self.name or self.email


@dataclass(slots=True)
class Folder:
    """A folder a note belongs to."""

    id: str = ""
    name: str = ""
    parent_folder_id: str | None = None

    @classmethod
    def from_api(cls, data: Any) -> Folder:
        """Build a folder from an API ``Folder`` object.

        Args:
            data: The decoded ``folder_membership[]`` entry.

        Returns:
            The parsed folder.
        """
        if not isinstance(data, dict):
            return cls()
        parent = _text(data.get("parent_folder_id")) or None
        return cls(
            id=_text(data.get("id")),
            name=_text(data.get("name")),
            parent_folder_id=parent,
        )


@dataclass(slots=True)
class CalendarEvent:
    """The calendar event a note was captured against."""

    event_title: str = ""
    # Deliberately British, against this project's US-English convention.
    # Granola's API spells the JSON key "organiser", so `_parse` must look it up
    # under that exact name, and `render` emits it verbatim as a `note.md`
    # frontmatter key -- 77 archived notes already carry it. Americanizing the
    # attribute alone would leave it mismatched with both; Americanizing all
    # three would break payload parsing and split the archive across two key
    # spellings. Left as-is on purpose; please do not "correct" it.
    organiser: str = ""
    invitees: list[str] = field(default_factory=list)
    calendar_event_id: str = ""
    scheduled_start_time: datetime | None = None
    scheduled_end_time: datetime | None = None

    @classmethod
    def from_api(cls, data: Any) -> CalendarEvent | None:
        """Build a calendar event from an API ``CalendarEvent`` object.

        Args:
            data: The decoded ``calendar_event`` field, which may be ``None``.

        Returns:
            The parsed event, or ``None`` when the note has no linked event.
        """
        if not isinstance(data, dict):
            return None
        invitees = data.get("invitees")
        return cls(
            event_title=_text(data.get("event_title")),
            organiser=_text(data.get("organiser")),
            invitees=[_text(i) for i in invitees if _text(i)]
            if isinstance(invitees, list)
            else [],
            calendar_event_id=_text(data.get("calendar_event_id")),
            scheduled_start_time=parse_timestamp(data.get("scheduled_start_time")),
            scheduled_end_time=parse_timestamp(data.get("scheduled_end_time")),
        )


@dataclass(slots=True)
class Speaker:
    """The attributed speaker of a transcript utterance."""

    source: str = ""
    diarization_label: str = ""
    name: str = ""

    @classmethod
    def from_api(cls, data: Any) -> Speaker:
        """Build a speaker from an API ``Speaker`` object.

        Args:
            data: The decoded ``transcript[].speaker`` entry.

        Returns:
            The parsed speaker.
        """
        if not isinstance(data, dict):
            return cls()
        return cls(
            source=_text(data.get("source")),
            diarization_label=_text(data.get("diarization_label")),
            name=_text(data.get("name")),
        )

    def label(self) -> str:
        """Pick the most specific speaker label available.

        Prefers an identified name, then the diarization label, then the audio
        source, so a transcript line is never left unattributed.

        Returns:
            A human-readable speaker label.
        """
        return self.name or self.diarization_label or self.source or "Unknown"


@dataclass(slots=True)
class Utterance:
    """A single timestamped transcript line."""

    speaker: Speaker = field(default_factory=Speaker)
    text: str = ""
    start_time: datetime | None = None
    end_time: datetime | None = None

    @classmethod
    def from_api(cls, data: Any) -> Utterance:
        """Build an utterance from an API ``Transcript`` item.

        Args:
            data: The decoded ``transcript[]`` entry.

        Returns:
            The parsed utterance.
        """
        if not isinstance(data, dict):
            return cls()
        return cls(
            speaker=Speaker.from_api(data.get("speaker")),
            text=_text(data.get("text")),
            start_time=parse_timestamp(data.get("start_time")),
            end_time=parse_timestamp(data.get("end_time")),
        )


@dataclass(slots=True)
class Note:
    """A Granola meeting note with its summary and optional transcript."""

    id: str = ""
    title: str = ""
    owner: User = field(default_factory=User)
    created_at: datetime | None = None
    updated_at: datetime | None = None
    web_url: str = ""
    calendar_event: CalendarEvent | None = None
    attendees: list[User] = field(default_factory=list)
    folder_membership: list[Folder] = field(default_factory=list)
    summary_text: str = ""
    summary_markdown: str = ""
    transcript: list[Utterance] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    source: str = SOURCE_PUBLIC_API
    degraded: bool = False
    date_text: str = ""

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> Note:
        """Build a note from an API ``Note`` object.

        Args:
            data: The decoded response of ``GET /v1/notes/{note_id}`` or an
                entry from ``GET /v1/notes``.

        Returns:
            The parsed note, retaining the original payload in ``raw``.
        """
        attendees = data.get("attendees")
        folders = data.get("folder_membership")
        transcript = data.get("transcript")
        return cls(
            id=_text(data.get("id")),
            title=_text(data.get("title")),
            owner=User.from_api(data.get("owner")),
            created_at=parse_timestamp(data.get("created_at")),
            updated_at=parse_timestamp(data.get("updated_at")),
            web_url=_text(data.get("web_url")),
            calendar_event=CalendarEvent.from_api(data.get("calendar_event")),
            attendees=[User.from_api(a) for a in attendees]
            if isinstance(attendees, list)
            else [],
            folder_membership=[Folder.from_api(f) for f in folders]
            if isinstance(folders, list)
            else [],
            summary_text=_text(data.get("summary_text")),
            summary_markdown=_text(data.get("summary_markdown")),
            transcript=[Utterance.from_api(u) for u in transcript]
            if isinstance(transcript, list)
            else [],
            raw=data,
        )

    @property
    def display_title(self) -> str:
        """The note title, falling back to a placeholder when untitled.

        Returns:
            A non-empty title suitable for filenames and headings.
        """
        return self.title or "Untitled"

    @property
    def uuid(self) -> str | None:
        """The internal UUID for this note, if one can be recovered.

        The public API identifies notes as ``not_*`` while the internal API and
        the Granola MCP use UUIDs. Where the payload carries a UUID -- either
        directly or embedded in ``web_url`` -- it is the join key for the
        optional enrichment pass.

        Returns:
            The UUID string, or ``None`` if the payload contains none.
        """
        for key in ("document_id", "uuid", "internal_id"):
            candidate = _text(self.raw.get(key))
            if UUID_RE.fullmatch(candidate):
                return candidate.lower()
        match = UUID_RE.search(self.web_url)
        return match.group(0).lower() if match else None
