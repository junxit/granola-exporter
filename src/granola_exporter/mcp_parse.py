"""Parsers for the Granola MCP's model-facing responses.

The MCP returns text shaped for a language model, not a versioned contract:
an XML-ish ``<meetings_data>`` block wrapped in a prompt-injection preamble,
with meeting summaries carrying arbitrary Markdown. None of that is guaranteed
to stay stable.

Three principles follow, and they are why this module is the way it is:

1. **Never guess.** A field is confidently extracted, or it is absent.
2. **Drift is loud.** An unrecognized shape raises. Returning "zero meetings"
   for an unparseable listing is indistinguishable from a genuinely empty
   window, and would let a sync report success while archiving nothing.
3. **Verbatim survives.** The original text is archived alongside the parse, so
   a parser fix can always be reapplied offline to the whole archive.

Everything here is a pure function over strings. Nothing does I/O, and nothing
imports the MCP SDK, so the whole module is testable offline.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from .models import (
    SOURCE_MCP,
    Folder,
    Note,
    Speaker,
    User,
    Utterance,
    is_valid_uuid,
)

PARSER_VERSION = 1

MEETINGS_ROOT = "meetings_data"

# A response larger than this is refused rather than parsed. The real payloads
# are a few hundred kilobytes at most.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

# No XML parser is used on this input at all. The payload is not well-formed
# XML anyway -- a prose preamble precedes the root and `<summary>` carries
# arbitrary Markdown -- so it is sliced structurally and attribute values are
# unescaped explicitly. That removes entity-expansion and XXE from the threat
# model by construction rather than by configuring a parser correctly, and
# avoids a `defusedxml` dependency for this alone.
#
# These declarations are still rejected outright: nothing legitimate in a
# Granola response contains them, so their presence means the payload is not
# what we think it is.
_FORBIDDEN_XML = ("<!DOCTYPE", "<!ENTITY", "<?xml-stylesheet")

# The five XML predefined entities, unescaped in the order that leaves a
# literal "&amp;lt;" in a meeting title intact.
_XML_ENTITIES = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&quot;", '"'),
    ("&apos;", "'"),
    ("&amp;", "&"),
)

_ATTR_RE = re.compile(r"""([A-Za-z_][\w:.\-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""")

_KNOWN_MEETING_ATTRS = frozenset(
    {
        "id",
        "title",
        "date",
        "captured_by_me",
        "listed_as_participant",
        "is_workspace_visible",
    }
)

_OPEN_TAG_RE = re.compile(r"<meeting\b[^>]*>", re.DOTALL)
_ROOT_OPEN_RE = re.compile(rf"<{MEETINGS_ROOT}\b[^>]*>", re.DOTALL)
_PARTICIPANTS_RE = re.compile(
    r"<known_participants>(.*?)</known_participants>", re.DOTALL
)
_SUMMARY_RE = re.compile(r"<summary>(.*?)</summary>", re.DOTALL)
_WS_RE = re.compile(r"\s+")


class MCPResponseFormatError(RuntimeError):
    """Raised when an MCP tool response does not match the expected shape."""

    def __init__(self, message: str, *, tool: str = "", excerpt: str = "") -> None:
        """Initialize the error.

        Args:
            message: What did not match.
            tool: The MCP tool whose response failed to parse.
            excerpt: A short slice of the offending payload, for diagnosis.
        """
        detail = f"[{tool}] {message}" if tool else message
        if excerpt:
            detail = f"{detail}\n  near: {excerpt[:200]!r}"
        super().__init__(detail)
        self.tool = tool


@dataclass(slots=True)
class MCPListingEnvelope:
    """The ``<meetings_data>`` wrapper's own attributes."""

    count: int
    date_from: str = ""
    date_to: str = ""


@dataclass(slots=True)
class MCPMeeting:
    """One ``<meeting>`` element, from either the listing or detail tool."""

    meeting_id: str
    title: str
    date_text: str
    element_text: str
    participants: list[str] = field(default_factory=list)
    summary_markdown: str = ""
    captured_by_me: bool | None = None
    listed_as_participant: bool | None = None
    is_workspace_visible: bool | None = None
    unknown_attrs: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class MCPTranscript:
    """The flat transcript string returned for one meeting."""

    meeting_id: str
    title: str
    text: str


@dataclass(slots=True)
class ParsedMCPDate:
    """A localized MCP date resolved, where possible, to a UTC instant."""

    instant: datetime | None
    text: str
    tz_resolved: bool = False


# Deliberately a North American table: these are the abbreviations Granola
# emits for this account. `CST` is ambiguous with China Standard Time and is
# resolved here as US Central. Anything absent is left unresolved rather than
# guessed, and the verbatim text is always preserved either way.
TZ_ABBREVIATIONS: dict[str, float] = {
    "UTC": 0.0,
    "GMT": 0.0,
    "Z": 0.0,
    "EST": -5.0,
    "EDT": -4.0,
    "CST": -6.0,
    "CDT": -5.0,
    "MST": -7.0,
    "MDT": -6.0,
    "PST": -8.0,
    "PDT": -7.0,
    "AKST": -9.0,
    "AKDT": -8.0,
    "HST": -10.0,
    "AST": -4.0,
    "NST": -3.5,
    "BST": 1.0,
    "CET": 1.0,
    "CEST": 2.0,
    "EET": 2.0,
    "EEST": 3.0,
}

_DATE_RE = re.compile(
    r"^(?P<naive>[A-Za-z]{3}\s+\d{1,2},\s+\d{4}\s+\d{1,2}:\d{2}\s*(?:AM|PM))"
    r"\s*(?P<tz>[A-Za-z]{1,5})?$"
)


def _guard(text: str, tool: str) -> None:
    """Refuse payloads that are oversized or carry a DTD.

    Args:
        text: The raw tool response.
        tool: The tool it came from, for the error message.

    Raises:
        MCPResponseFormatError: If the payload is too large or contains a
            document type or entity declaration.
    """
    if len(text) > MAX_RESPONSE_BYTES:
        raise MCPResponseFormatError(
            f"response of {len(text)} bytes exceeds the {MAX_RESPONSE_BYTES} limit",
            tool=tool,
        )
    upper = text.upper()
    for token in _FORBIDDEN_XML:
        if token in upper:
            raise MCPResponseFormatError(
                f"refusing a payload containing {token}", tool=tool
            )


def _extract_block(text: str, tool: str) -> str:
    """Slice the ``<meetings_data>`` element out of a tool response.

    The response is prefixed with a prose preamble telling the reader to treat
    the content as data, so the block is located rather than parsed from the
    start of the string.

    Args:
        text: The raw tool response.
        tool: The tool it came from.

    Returns:
        The ``<meetings_data> … </meetings_data>`` substring.

    Raises:
        MCPResponseFormatError: If the wrapper is missing.
    """
    _guard(text, tool)
    open_match = _ROOT_OPEN_RE.search(text)
    if not open_match:
        raise MCPResponseFormatError(
            f"no <{MEETINGS_ROOT}> element in the response",
            tool=tool,
            excerpt=text[:200],
        )
    closing = f"</{MEETINGS_ROOT}>"
    end = text.rfind(closing)
    if end == -1:
        # A self-closing empty envelope is legitimate.
        if open_match.group(0).rstrip().endswith("/>"):
            return open_match.group(0)
        raise MCPResponseFormatError(
            f"unterminated <{MEETINGS_ROOT}> element",
            tool=tool,
            excerpt=text[-200:],
        )
    return text[open_match.start() : end + len(closing)]


def _xml_unescape(text: str) -> str:
    """Unescape only the five XML predefined entities.

    Deliberately narrower than ``html.unescape``, which would also expand
    things like ``&copy;`` that XML leaves alone -- silently rewriting the
    contents of a meeting title this tool is supposed to archive verbatim.

    Args:
        text: An escaped attribute value or element body.

    Returns:
        The unescaped text.
    """
    for entity, char in _XML_ENTITIES:
        text = text.replace(entity, char)
    return text


def _attrs_of(tag: str) -> dict[str, str]:
    """Read the attributes of a single opening tag.

    Args:
        tag: An opening tag such as ``<meeting id="…" title="…">``.

    Returns:
        The tag's attributes, unescaped. A malformed tag simply yields fewer
        attributes; callers raise on the required ones being absent, so drift
        still surfaces as an error rather than as silent data loss.
    """
    return {
        match.group(1): _xml_unescape(
            match.group(2) if match.group(2) is not None else match.group(3)
        )
        for match in _ATTR_RE.finditer(tag)
    }


def _bool_attr(value: str | None) -> bool | None:
    """Interpret an MCP boolean attribute.

    Args:
        value: The raw attribute value, or ``None`` when absent.

    Returns:
        The boolean, or ``None`` when absent or unrecognized.
    """
    if value is None:
        return None
    lowered = value.strip().lower()
    if lowered in {"true", "1"}:
        return True
    if lowered in {"false", "0"}:
        return False
    return None


def _participants(chunk: str) -> list[str]:
    """Extract the participant labels from a meeting element.

    Args:
        chunk: The verbatim ``<meeting>`` element text.

    Returns:
        One entry per participant, e.g. ``Jade Naaman <jade@example.com>``.
    """
    match = _PARTICIPANTS_RE.search(chunk)
    if not match:
        return []
    body = _xml_unescape(match.group(1))
    return [part.strip() for part in body.split(",") if part.strip()]


def _parse_meetings(text: str, tool: str) -> tuple[MCPListingEnvelope, list[MCPMeeting]]:
    """Parse a ``<meetings_data>`` response into meetings.

    ``<meeting>`` elements do not nest, so the block is split on the closing
    tag and only each opening tag's attributes are XML-parsed. Free text --
    participant lists and Markdown summaries -- is taken as a raw slice and
    unescaped, never handed to the XML parser.

    Args:
        text: The raw tool response.
        tool: The tool it came from.

    Returns:
        The envelope and the meetings it declared.

    Raises:
        MCPResponseFormatError: If the wrapper is missing, the declared count
            disagrees with the number of elements found, or a required
            attribute is absent.
    """
    block = _extract_block(text, tool)
    root_attrs = _attrs_of(_ROOT_OPEN_RE.search(block).group(0))

    raw_count = root_attrs.get("count")
    try:
        declared = int(raw_count) if raw_count is not None else -1
    except ValueError as exc:
        raise MCPResponseFormatError(
            f"non-numeric count attribute {raw_count!r}", tool=tool
        ) from exc

    meetings: list[MCPMeeting] = []
    for chunk in block.split("</meeting>")[:-1]:
        open_match = _OPEN_TAG_RE.search(chunk)
        if not open_match:
            continue
        element_text = chunk[open_match.start() :] + "</meeting>"
        attrs = _attrs_of(open_match.group(0))

        meeting_id = (attrs.get("id") or "").strip().lower()
        title = (attrs.get("title") or "").strip()
        date_text = (attrs.get("date") or "").strip()
        if not meeting_id or not title or not date_text:
            raise MCPResponseFormatError(
                "meeting element missing id, title or date",
                tool=tool,
                excerpt=open_match.group(0),
            )

        summary = _SUMMARY_RE.search(chunk)
        meetings.append(
            MCPMeeting(
                meeting_id=meeting_id,
                title=title,
                date_text=date_text,
                element_text=element_text,
                participants=_participants(chunk),
                summary_markdown=(
                    _xml_unescape(summary.group(1)).strip() if summary else ""
                ),
                captured_by_me=_bool_attr(attrs.get("captured_by_me")),
                listed_as_participant=_bool_attr(attrs.get("listed_as_participant")),
                is_workspace_visible=_bool_attr(attrs.get("is_workspace_visible")),
                unknown_attrs={
                    k: v for k, v in attrs.items() if k not in _KNOWN_MEETING_ATTRS
                },
            )
        )

    if declared >= 0 and declared != len(meetings):
        # The server hands us a checksum; a mismatch means our slicing lost or
        # invented an element, which for an archive is worse than an error.
        raise MCPResponseFormatError(
            f"count attribute says {declared} but {len(meetings)} were parsed",
            tool=tool,
        )

    return (
        MCPListingEnvelope(
            count=len(meetings),
            date_from=root_attrs.get("from", ""),
            date_to=root_attrs.get("to", ""),
        ),
        meetings,
    )


def parse_meetings_listing(text: str) -> tuple[MCPListingEnvelope, list[MCPMeeting]]:
    """Parse a ``list_meetings`` response.

    Args:
        text: The raw tool response.

    Returns:
        The envelope and the listed meetings.
    """
    return _parse_meetings(text, "list_meetings")


def parse_meetings_detail(text: str) -> tuple[MCPListingEnvelope, list[MCPMeeting]]:
    """Parse a ``get_meetings`` response, including summaries.

    Args:
        text: The raw tool response.

    Returns:
        The envelope and the detailed meetings.
    """
    return _parse_meetings(text, "get_meetings")


def parse_transcript(payload: Any) -> MCPTranscript:
    """Parse a ``get_meeting_transcript`` response.

    Args:
        payload: The decoded JSON object returned by the tool.

    Returns:
        The transcript, with its flat text preserved verbatim.

    Raises:
        MCPResponseFormatError: If the payload is not an object carrying an id.
    """
    if not isinstance(payload, dict):
        raise MCPResponseFormatError(
            f"expected an object, got {type(payload).__name__}",
            tool="get_meeting_transcript",
        )
    meeting_id = str(payload.get("id") or "").strip().lower()
    if not is_valid_uuid(meeting_id):
        raise MCPResponseFormatError(
            f"transcript carries a non-UUID id {meeting_id!r}",
            tool="get_meeting_transcript",
        )
    return MCPTranscript(
        meeting_id=meeting_id,
        title=str(payload.get("title") or "").strip(),
        text=str(payload.get("transcript") or ""),
    )


def listing_hash(element_text: str) -> str:
    """Digest a ``<meeting>`` element for cheap change detection.

    This is the MCP's stand-in for the public API stub's ``updated_at``: the
    listing tool returns title, date and participants, so a change in any of
    them shows up here without paying for a detail call.

    Args:
        element_text: The verbatim meeting element.

    Returns:
        A hex SHA-256 digest over the whitespace-normalized element.
    """
    normalized = _WS_RE.sub(" ", element_text).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


SPEAKER_LABEL_MAX_LEN = 40
SPEAKER_LABEL_MIN_OCCURRENCES = 2
_ALWAYS_LABELS = frozenset({"Me", "Them"})

# A speaker label is one to four capitalised words. Allowing arbitrary
# characters lets the match run across a sentence boundary to the next colon --
# "Hey, Oat. How's it going?  Me" becomes one 29-character "label", swallowing
# the real one. Requiring capitalised words also rejects "3:30" and "https:"
# for free.
_CANDIDATE_RE = re.compile(
    r"(?:(?<=\s)|^)((?:[A-Z][\w'\-]*)(?:[ \t]+[A-Z][\w'\-]*){0,3}):[ \t]"
)


def _accepted_labels(text: str) -> set[str]:
    """Discover the speaker labels actually used in a transcript.

    Splitting on any capitalised word before a colon would mangle ordinary
    prose ("Note: ..."), clock times ("3:30") and URLs, so labels are drawn
    from a vocabulary discovered in the document itself.

    Because the transcript is one unpunctuated run, a label is often abutted by
    the last word of the previous turn -- "... How are you? All  Them: ...".
    Every trailing word-suffix of a candidate is therefore counted too, so the
    real ``Them`` is found inside the spurious ``All Them``.

    Args:
        text: The flat transcript string.

    Returns:
        The labels considered real speakers, whitespace-normalized.
    """
    counts: dict[str, int] = {}
    for match in _CANDIDATE_RE.finditer(text):
        words = match.group(1).split()
        for start in range(len(words)):
            candidate = " ".join(words[start:])
            counts[candidate] = counts.get(candidate, 0) + 1

    accepted = set()
    for label, count in counts.items():
        if label in _ALWAYS_LABELS:
            accepted.add(label)
            continue
        if len(label) > SPEAKER_LABEL_MAX_LEN:
            continue
        if any(ch.isdigit() for ch in label) or "/" in label:
            continue
        # A real speaker speaks more than once; a stray colon does not.
        if count >= SPEAKER_LABEL_MIN_OCCURRENCES:
            accepted.add(label)
    return accepted


def _label_pattern(labels: set[str]) -> re.Pattern[str]:
    """Build a regex matching exactly the accepted labels before a colon.

    Args:
        labels: The accepted speaker labels.

    Returns:
        A compiled pattern capturing the label. Longer labels are tried first
        so ``Oat Benson`` wins over a bare ``Oat``.
    """
    alternatives = [
        r"\s+".join(re.escape(word) for word in label.split())
        for label in sorted(labels, key=lambda x: (-len(x.split()), -len(x)))
    ]
    return re.compile(
        r"(?:(?<=\s)|^)(" + "|".join(alternatives) + r"):[ \t]"
    )


def split_transcript(text: str) -> list[Utterance]:
    """Split a flat MCP transcript into speaker-attributed utterances.

    The MCP returns one unsegmented string with inline ``Me:``/``Them:``/
    ``Name:`` labels and no timing information, so the result carries no
    timestamps. ``render.render_transcript`` already degrades gracefully when
    ``start_time`` is ``None``.

    Args:
        text: The flat transcript string.

    Returns:
        Utterances in order. When no label is confidently identified the whole
        text is returned as a single ``Unknown`` utterance -- content is never
        dropped just because it could not be attributed.
    """
    body = text.strip()
    if not body:
        return []

    labels = _accepted_labels(text)
    if not labels:
        return [Utterance(speaker=Speaker(name="Unknown"), text=body)]

    cuts = [
        (m.start(1), m.end(), " ".join(m.group(1).split()))
        for m in _label_pattern(labels).finditer(text)
    ]
    if not cuts:
        return [Utterance(speaker=Speaker(name="Unknown"), text=body)]

    utterances: list[Utterance] = []
    preamble = text[: cuts[0][0]].strip()
    if preamble:
        utterances.append(Utterance(speaker=Speaker(name="Unknown"), text=preamble))

    for index, (_, content_start, label) in enumerate(cuts):
        content_end = cuts[index + 1][0] if index + 1 < len(cuts) else len(text)
        spoken = text[content_start:content_end].strip()
        if spoken:
            utterances.append(Utterance(speaker=Speaker(name=label), text=spoken))
    return utterances


def parse_mcp_date(text: str) -> ParsedMCPDate:
    """Resolve a localized MCP date string to a UTC instant.

    ``strptime`` cannot reliably consume ``%Z`` for arbitrary abbreviations, so
    the naive portion is parsed on its own and the trailing abbreviation is
    mapped through :data:`TZ_ABBREVIATIONS`.

    Args:
        text: A string such as ``"Jul 28, 2026 8:00 PM CDT"``.

    Returns:
        The parse. An unmapped abbreviation leaves ``instant`` as ``None``
        rather than guessing: an undated note is a visible defect, a
        confidently wrongly-dated one is not.
    """
    raw = (text or "").strip()
    match = _DATE_RE.match(raw)
    if not match:
        return ParsedMCPDate(instant=None, text=raw)

    try:
        naive = datetime.strptime(
            _WS_RE.sub(" ", match.group("naive")).strip(), "%b %d, %Y %I:%M %p"
        )
    except ValueError:
        return ParsedMCPDate(instant=None, text=raw)

    abbreviation = (match.group("tz") or "").upper()
    if not abbreviation:
        return ParsedMCPDate(instant=naive.replace(tzinfo=UTC), text=raw, tz_resolved=False)
    if abbreviation not in TZ_ABBREVIATIONS:
        return ParsedMCPDate(instant=None, text=raw)

    offset = timezone(timedelta(hours=TZ_ABBREVIATIONS[abbreviation]))
    return ParsedMCPDate(
        instant=naive.replace(tzinfo=offset).astimezone(UTC),
        text=raw,
        tz_resolved=True,
    )


def _user_from_label(label: str) -> User:
    """Split a ``Name <email>`` participant label into a user.

    Args:
        label: A participant entry from ``<known_participants>``.

    Returns:
        The parsed user.
    """
    match = re.match(r"^(?P<name>.*?)\s*<(?P<email>[^>]+)>\s*$", label.strip())
    if not match:
        return User(name=label.strip())
    name = match.group("name").strip()
    name = re.sub(r"\s*\(note creator\)$", "", name).strip()
    return User(name=name, email=match.group("email").strip())


def build_raw(
    meeting: MCPMeeting,
    listing_element: str,
    transcript: MCPTranscript | None,
    folder_names: list[str],
    server_url: str,
) -> dict[str, Any]:
    """Assemble the ``raw.json`` wrapper archived for an MCP note.

    The verbatim tool output is stored alongside the parse so that a parser fix
    can be reapplied offline, exactly as ``raw.json`` already lets a template
    change be reapplied without refetching.

    Note there is deliberately **no fetch timestamp** in here: it would change
    on every run, so the content hash would too, and every note would look
    updated forever. Fetch times belong in the index.

    Args:
        meeting: The parsed detail element.
        listing_element: The verbatim element from the listing tool.
        transcript: The transcript payload, when one was fetched.
        folder_names: Folder names this meeting belongs to.
        server_url: The MCP endpoint the data came from.

    Returns:
        The ``raw.json`` payload.
    """
    return {
        "object": "granola_mcp_note",
        "schema_version": 1,
        "source": SOURCE_MCP,
        "degraded": True,
        "parser_version": PARSER_VERSION,
        "uuid": meeting.meeting_id,
        "mcp_server": server_url,
        "mcp": {
            "list_meetings_element": listing_element,
            "get_meetings_element": meeting.element_text,
            "get_meeting_transcript": (
                {
                    "id": transcript.meeting_id,
                    "title": transcript.title,
                    "transcript": transcript.text,
                }
                if transcript
                else None
            ),
            "folder_names": sorted(folder_names),
        },
    }


def build_note(
    meeting: MCPMeeting,
    raw: dict[str, Any],
    *,
    transcript: MCPTranscript | None,
    folder_names: list[str],
) -> Note:
    """Project MCP tool output onto the public-API-shaped ``Note`` model.

    Everything downstream -- rendering, the archive layout, the index -- then
    works unchanged.

    ``updated_at`` is deliberately left ``None``: the MCP exposes no update
    time, and inventing one would poison the index and could mislead a later
    public API sync into thinking the note was already current.

    Args:
        meeting: The parsed detail element.
        raw: The wrapper from :func:`build_raw`, archived verbatim.
        transcript: The transcript payload, when one was fetched.
        folder_names: Folder names this meeting belongs to.

    Returns:
        The projected note.
    """
    parsed_date = parse_mcp_date(meeting.date_text)
    return Note(
        id=f"mcp_{meeting.meeting_id}",
        title=meeting.title,
        created_at=parsed_date.instant,
        updated_at=None,
        # Synthesised, not returned by any tool. Safe because the id
        # equivalence with the public API's web_url is verified.
        web_url=f"https://notes.granola.ai/d/{meeting.meeting_id}",
        attendees=[_user_from_label(p) for p in meeting.participants],
        # Only folder *names* cross the backend boundary: MCP folder ids are
        # UUIDs in a different namespace from the public API's `fol_*`.
        folder_membership=[Folder(name=n) for n in sorted(folder_names)],
        summary_markdown=meeting.summary_markdown,
        transcript=split_transcript(transcript.text) if transcript else [],
        raw=raw,
        source=SOURCE_MCP,
        degraded=True,
        date_text=meeting.date_text,
    )
