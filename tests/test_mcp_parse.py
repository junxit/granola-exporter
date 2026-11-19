"""Tests for the Granola MCP response parsers.

These are the highest-value tests in the suite: the MCP returns model-facing
prose rather than a versioned contract, so this is where drift has to surface.
Everything here is offline and imports no SDK.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from granola_exporter.mcp_parse import (
    MCPResponseFormatError,
    build_note,
    build_raw,
    listing_hash,
    parse_mcp_date,
    parse_meetings_detail,
    parse_meetings_listing,
    parse_transcript,
    split_transcript,
)
from granola_exporter.models import SOURCE_MCP

UUID_A = "d290f1ee-6c54-4b01-90e6-d701748f0851"
UUID_B = "e96c1c66-ed02-4a32-9acd-54720f8761b1"
UUID_C = "7c4f0a52-3b1e-4d8a-9f60-2e5b8c1d0a93"


# -- listing ---------------------------------------------------------------


def test_listing_round_trips_every_attribute(mcp_listing_text):
    """The measured response shape parses attribute for attribute."""
    envelope, meetings = parse_meetings_listing(mcp_listing_text)

    assert envelope.count == 2
    assert envelope.date_from == "Jan 27, 2026"
    assert [m.meeting_id for m in meetings] == [UUID_A, UUID_B]

    first, second = meetings
    assert first.title == "Quarterly yoghurt budget: review"
    assert first.date_text == "Jan 27, 2026 9:30 AM CST"
    assert first.captured_by_me is True
    assert first.is_workspace_visible is False
    assert first.participants == [
        "Oat Benson (note creator) <oat@granola.ai>",
        "Milk Jones <milk@granola.ai>",
    ]
    assert second.title == "Yoghurt & Granola sync", "XML entities must be unescaped"
    assert second.listed_as_participant is False


def test_empty_window_is_not_an_error(mcp_empty_listing_text):
    """A genuinely empty range returns no meetings rather than raising.

    The distinction matters: treating an unparseable response as "no meetings"
    would let a sync report success while archiving nothing.
    """
    envelope, meetings = parse_meetings_listing(mcp_empty_listing_text)
    assert envelope.count == 0
    assert meetings == []


def test_missing_envelope_raises():
    """Drift that removes the wrapper is loud, not silently empty."""
    with pytest.raises(MCPResponseFormatError, match="no <meetings_data>"):
        parse_meetings_listing("Sorry, I could not find any meetings.")


def test_count_mismatch_raises(mcp_listing_text):
    """The server's own count is used as a checksum on our slicing."""
    tampered = mcp_listing_text.replace('count="2"', 'count="3"')
    with pytest.raises(MCPResponseFormatError, match="count attribute says 3"):
        parse_meetings_listing(tampered)


def test_meeting_missing_a_required_attribute_raises(mcp_listing_text):
    """A meeting without a date cannot be filed, so it must not be guessed."""
    broken = mcp_listing_text.replace(' date="Jan 27, 2026 9:30 AM CST"', "", 1)
    with pytest.raises(MCPResponseFormatError, match="missing id, title or date"):
        parse_meetings_listing(broken)


def test_unknown_attributes_are_surfaced_not_fatal(mcp_listing_text):
    """Additive drift is a warning; it must not break the archive."""
    extended = mcp_listing_text.replace(
        'captured_by_me="true"', 'captured_by_me="true" sentiment="warm"', 1
    )
    _, meetings = parse_meetings_listing(extended)
    assert meetings[0].unknown_attrs == {"sentiment": "warm"}


@pytest.mark.parametrize("token", ["<!DOCTYPE foo>", "<!ENTITY lol 'x'>"])
def test_entity_declarations_are_refused(mcp_listing_text, token):
    """Entity-expansion constructs are rejected before any parsing."""
    with pytest.raises(MCPResponseFormatError, match="refusing a payload"):
        parse_meetings_listing(token + mcp_listing_text)


def test_oversized_response_is_refused():
    """A payload far larger than any real response is refused."""
    with pytest.raises(MCPResponseFormatError, match="exceeds"):
        parse_meetings_listing("x" * (8 * 1024 * 1024 + 1))


# -- the current listing shape ---------------------------------------------


def test_current_listing_is_dated_by_meeting_at(mcp_current_listing_text):
    """Regression: the server dropped ``date``, and every sync stopped.

    Each meeting now carries only an ISO 8601 ``meeting_at``. Requiring
    ``date`` raised on the first meeting of the first window.
    """
    envelope, meetings = parse_meetings_listing(mcp_current_listing_text)

    assert envelope.count == 2
    assert [m.meeting_id for m in meetings] == [UUID_A, UUID_B]
    first, second = meetings
    assert first.date_text == "2026-01-27T09:30:00-06:00"
    assert second.date_text == "2026-01-28T20:00:00.000-05:00"
    assert "meeting_at" not in first.unknown_attrs
    assert first.unknown_attrs == {"organizer": "oat@granola.ai"}


def test_flags_left_off_a_meeting_take_the_defaults(mcp_current_listing_text):
    """A flag matching ``flag_defaults`` is omitted, not unknown."""
    _, (first, second) = parse_meetings_listing(mcp_current_listing_text)

    assert (first.captured_by_me, first.listed_as_participant) == (True, True)
    assert first.is_workspace_visible is False
    assert second.captured_by_me is True, "omitted, so the default"
    assert second.listed_as_participant is False, "stated, so it wins"
    assert second.is_workspace_visible is True


def test_a_paged_listing_reports_its_cursor(mcp_current_listing_text):
    """The cursor and total come from the element ahead of the root."""
    envelope, _ = parse_meetings_listing(mcp_current_listing_text)

    assert envelope.paginated is True
    assert envelope.has_more is True
    assert envelope.next_cursor == "cursor-page-2"
    assert envelope.total_count == 3


def test_an_unpaged_listing_reports_no_cursor(mcp_listing_text):
    """A listing without a pagination element is a single page."""
    envelope, _ = parse_meetings_listing(mcp_listing_text)

    assert envelope.paginated is False
    assert envelope.has_more is False
    assert envelope.total_count is None
    assert envelope.duplicates == []


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (' next_cursor="cursor-page-2"', ""),
        ('has_more="true"', 'has_more="maybe"'),
    ],
)
def test_a_cursor_that_cannot_be_followed_raises(mcp_current_listing_text, before, after):
    """More pages without a way to reach them must not read as the last page."""
    broken = mcp_current_listing_text.replace(before, after, 1)
    with pytest.raises(MCPResponseFormatError, match="pagination"):
        parse_meetings_listing(broken)


def test_a_non_numeric_total_raises(mcp_current_listing_text):
    """The total is the completeness check, so it must be a number."""
    broken = mcp_current_listing_text.replace('total_count="3"', 'total_count="lots"')
    with pytest.raises(MCPResponseFormatError, match="total_count"):
        parse_meetings_listing(broken)


def test_a_meeting_named_only_as_a_duplicate_is_returned(mcp_current_listing_text):
    """Another capture of a listed event is a note of its own, so it comes back.

    It has no element to parse, so it carries the group's tag and the date of
    the event it shares with the listed meeting.
    """
    envelope, meetings = parse_meetings_listing(mcp_current_listing_text)

    (duplicate,) = envelope.duplicates
    assert duplicate.meeting_id == UUID_C
    assert duplicate.duplicate_of == UUID_A
    assert duplicate.date_text == meetings[0].date_text
    assert duplicate.element_text == f'<meeting id="{UUID_A}" other_ids="{UUID_C}" />'
    assert duplicate.title == ""


def test_a_duplicate_that_is_listed_is_not_repeated(mcp_current_listing_text):
    """An id the root already lists is not a second copy of anything."""
    both = mcp_current_listing_text.replace(
        f'other_ids="{UUID_C}"', f'other_ids="{UUID_B},{UUID_C}"'
    )
    envelope, _ = parse_meetings_listing(both)
    assert [d.meeting_id for d in envelope.duplicates] == [UUID_C]


def test_a_duplicate_group_naming_no_others_raises(mcp_current_listing_text):
    """An empty group is drift, not a group of one."""
    broken = mcp_current_listing_text.replace(f' other_ids="{UUID_C}"', "")
    with pytest.raises(MCPResponseFormatError, match="other_ids"):
        parse_meetings_listing(broken)


def test_tags_quoted_in_a_summary_are_not_taken_for_real(mcp_detail_text):
    """A summary is free Markdown, so it may quote either tag verbatim."""
    quoting = mcp_detail_text.replace(
        "### Decisions",
        '### Decisions\n\n<pagination has_more="true" next_cursor="x" />\n'
        f'<duplicate_recordings><meeting id="{UUID_A}" other_ids="{UUID_C}" />'
        "</duplicate_recordings>",
    )
    envelope, meetings = parse_meetings_detail(quoting)

    assert envelope.paginated is False
    assert envelope.duplicates == []
    assert "<pagination" in meetings[0].summary_markdown


def test_date_wins_where_both_dates_appear(mcp_listing_text):
    """Elements archived from the older server parse exactly as they did."""
    both = mcp_listing_text.replace(
        'date="Jan 27, 2026 9:30 AM CST"',
        'date="Jan 27, 2026 9:30 AM CST" meeting_at="2026-01-27T09:30:00.000-06:00"',
        1,
    )
    _, meetings = parse_meetings_listing(both)
    assert meetings[0].date_text == "Jan 27, 2026 9:30 AM CST"


def test_a_meeting_with_neither_date_raises(mcp_current_listing_text):
    """Without ``date`` or ``meeting_at`` a meeting cannot be filed."""
    broken = mcp_current_listing_text.replace(
        ' meeting_at="2026-01-27T09:30:00-06:00"', "", 1
    )
    with pytest.raises(MCPResponseFormatError, match="missing id, title or date"):
        parse_meetings_listing(broken)


def test_current_detail_parses(mcp_current_detail_text):
    """The detail shape gained elements; none of them get in the way."""
    envelope, (meeting,) = parse_meetings_detail(mcp_current_detail_text)

    assert envelope.count == 1
    assert meeting.date_text == "2026-01-27T09:30:00-06:00"
    assert meeting.summary_markdown == "### Decisions\n\n- Approved the budget."
    assert meeting.participants == ["Oat Benson (note creator) <oat@granola.ai>"]


# -- detail ----------------------------------------------------------------


def test_detail_summary_survives_hostile_markdown(mcp_detail_text):
    """A summary containing bare ``<``, ``&`` and a code fence parses intact.

    This is why the summary is sliced rather than XML-parsed.
    """
    envelope, meetings = parse_meetings_detail(mcp_detail_text)

    assert envelope.count == 1
    summary = meetings[0].summary_markdown
    assert "### Decisions" in summary
    assert "spend < 5 & margin > 2" in summary, "entities must be unescaped once"
    assert "`a < b && c`" in summary, "a bare < inside code must survive"
    assert "<div>not a real tag</div>" in summary


def test_literal_ampersand_entity_is_not_double_unescaped():
    """``&amp;lt;`` must become ``&lt;``, not ``<``."""
    text = (
        '<meetings_data count="1">\n'
        f'<meeting id="{UUID_A}" title="A &amp;lt; B" date="Jan 27, 2026 9:30 AM CST">'
        "</meeting>\n</meetings_data>"
    )
    _, meetings = parse_meetings_detail(text)
    assert meetings[0].title == "A &lt; B"


# -- transcript ------------------------------------------------------------


def test_transcript_splits_on_me_and_them(mcp_transcript_payload):
    """The flat string becomes attributed utterances."""
    transcript = parse_transcript(mcp_transcript_payload)
    utterances = split_transcript(transcript.text)

    labels = [u.speaker.label() for u in utterances]
    assert labels == ["Them", "Me", "Them", "Me", "Them"]
    assert utterances[0].text == "Hey, Oat. How's it going?"
    assert all(u.start_time is None for u in utterances), "MCP has no timestamps"


def test_transcript_does_not_split_on_prose_times_or_urls(mcp_transcript_payload):
    """"Note:", "3:30" and "https://" must not be mistaken for speakers.

    A naive ``(\\w+):`` split mangles all three.
    """
    utterances = split_transcript(mcp_transcript_payload["transcript"])
    joined = " ".join(u.text for u in utterances)
    assert "Note: the budget doubled." in joined
    assert "3:30" in joined
    assert "https://example.com/x" in joined
    assert {u.speaker.label() for u in utterances} == {"Me", "Them"}


def test_named_speaker_needs_more_than_one_appearance():
    """A real speaker speaks twice; a stray colon does not."""
    once = "Kay: hello there. And then something else happened."
    assert {u.speaker.label() for u in split_transcript(once)} == {"Unknown"}

    twice = "Kay: hello there. Me: hi. Kay: how are you?"
    assert {u.speaker.label() for u in split_transcript(twice)} == {"Kay", "Me"}


def test_label_abutted_by_the_previous_turn_is_still_found():
    """Regression, from a real transcript.

    These strings are one unpunctuated run, so a label is often preceded by a
    trailing word: "How are you? All  Them: All well." A word-sequence match
    captures "All Them", which appears once and is discarded -- silently
    merging two speakers' turns. Suffixes of a candidate are counted too.
    """
    text = (
        " Them: Hey, Jade. How's it going?  Me: Great. How are you? All "
        " Them: All well. Thank you.  Me: thank you all then. "
    )
    utterances = split_transcript(text)
    assert [u.speaker.label() for u in utterances] == ["Them", "Me", "Them", "Me"]
    assert utterances[2].text.startswith("All well.")


def test_multi_word_speaker_beats_its_own_suffix():
    """A two-word name must not be split down to its last word."""
    text = "Oat Benson: hello. Milk Jones: hi there. Oat Benson: bye. Milk Jones: bye."
    labels = [u.speaker.label() for u in split_transcript(text)]
    assert labels == ["Oat Benson", "Milk Jones", "Oat Benson", "Milk Jones"]


def test_unlabeled_transcript_keeps_its_content():
    """Content is never dropped just because it cannot be attributed."""
    utterances = split_transcript("just some words with no labels at all")
    assert len(utterances) == 1
    assert utterances[0].speaker.label() == "Unknown"
    assert utterances[0].text == "just some words with no labels at all"


def test_empty_transcript_yields_nothing():
    """An empty string is not an utterance."""
    assert split_transcript("") == []
    assert split_transcript("   ") == []


def test_transcript_with_a_non_uuid_id_raises():
    """A transcript id becomes an archive key, so it is validated."""
    with pytest.raises(MCPResponseFormatError, match="non-UUID id"):
        parse_transcript({"id": "../../etc/passwd", "transcript": "x"})


# -- dates -----------------------------------------------------------------


def test_localized_date_resolves_to_utc():
    """The measured discrepancy: 8pm CDT Jul 28 is Jul 29 in UTC."""
    parsed = parse_mcp_date("Jul 28, 2026 8:00 PM CDT")
    assert parsed.tz_resolved
    assert parsed.instant == datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
    assert parsed.text == "Jul 28, 2026 8:00 PM CDT", "verbatim text is preserved"


def test_unknown_timezone_is_left_unresolved():
    """An unmapped abbreviation must not be guessed.

    An undated note is a visible defect; a confidently wrong date is not.
    """
    parsed = parse_mcp_date("Jul 28, 2026 8:00 PM XYZ")
    assert parsed.instant is None
    assert parsed.tz_resolved is False
    assert parsed.text == "Jul 28, 2026 8:00 PM XYZ"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not a date",
        "2026-07-28",
        "2026-13-40T99:99:00Z",
        "Jul 99, 2026 8:00 PM CDT",
    ],
)
def test_unparseable_dates_are_left_unresolved(value):
    """Anything unrecognized yields no instant rather than an exception.

    A bare date names a day in some timezone, not an instant, so it is not
    resolved either.
    """
    assert parse_mcp_date(value).instant is None


@pytest.mark.parametrize(
    "value",
    [
        "2026-07-28T20:00:00-05:00",
        "2026-07-28T20:00:00.000-05:00",
        "2026-07-29T01:00:00Z",
        "2026-07-29T01:00:00+00:00",
    ],
)
def test_meeting_at_resolves_to_utc(value):
    """``meeting_at`` carries its own offset, so it always resolves."""
    parsed = parse_mcp_date(value)
    assert parsed.instant == datetime(2026, 7, 29, 1, 0, tzinfo=UTC)
    assert parsed.tz_resolved is True
    assert parsed.text == value, "the original is kept verbatim"


def test_meeting_at_without_an_offset_is_unresolved():
    """A local time with no offset is read as UTC and says so."""
    parsed = parse_mcp_date("2026-07-28T20:00:00")
    assert parsed.instant == datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
    assert parsed.tz_resolved is False


# -- hashing and projection ------------------------------------------------


def test_listing_hash_ignores_whitespace_but_not_content(mcp_listing_text):
    """The hash is the MCP's stand-in for the stub's ``updated_at``."""
    _, meetings = parse_meetings_listing(mcp_listing_text)
    original = meetings[0].element_text

    assert listing_hash(original) == listing_hash(
        original.replace("\n", "\n   ")
    ), "reflowing must not look like a change"
    assert listing_hash(original) != listing_hash(
        original.replace("Quarterly", "Monthly")
    ), "a retitle must look like a change"


def test_build_note_projects_onto_the_shared_model(
    mcp_detail_text, mcp_transcript_payload
):
    """The projection is what lets render and store work unchanged."""
    _, meetings = parse_meetings_detail(mcp_detail_text)
    meeting = meetings[0]
    transcript = parse_transcript(mcp_transcript_payload)
    raw = build_raw(meeting, meeting.element_text, transcript, ["Projects"], "https://m")
    note = build_note(
        meeting, raw, transcript=transcript, folder_names=["Projects"]
    )

    assert note.id == f"mcp_{UUID_A}"
    assert note.source == SOURCE_MCP
    assert note.degraded is True
    assert note.updated_at is None, "an invented updated_at would poison the index"
    assert note.uuid == UUID_A, "the join key must survive the projection"
    assert note.web_url == f"https://notes.granola.ai/d/{UUID_A}"
    assert note.date_text == "Jan 27, 2026 9:30 AM CST"
    assert [f.name for f in note.folder_membership] == ["Projects"]
    assert note.attendees[0].email == "oat@granola.ai"
    assert note.attendees[0].name == "Oat Benson", "(note creator) is stripped"


def test_raw_has_no_fetch_timestamp(mcp_detail_text):
    """A timestamp in raw.json would change the hash on every single run.

    Every note would then look updated forever, defeating change detection.
    """
    _, meetings = parse_meetings_detail(mcp_detail_text)
    raw = build_raw(meetings[0], meetings[0].element_text, None, [], "https://m")

    flattened = repr(raw)
    assert "fetched_at" not in flattened
    assert "archived_at" not in flattened
    assert raw["mcp"]["get_meetings_element"] == meetings[0].element_text
