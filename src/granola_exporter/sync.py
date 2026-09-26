"""Sync orchestration, one function per backend.

Each ``sync_*`` function takes an **already-constructed** client rather than
building one. That injection is what lets the sync loop be driven end to end
by a fake in tests, with no network, no credentials and no SDK import.

Durability lives here rather than in the caller: the index is saved before any
exception leaves this module, so an interrupted or failed run never loses the
notes it already archived.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from .mcp_parse import (
    PARSER_VERSION,
    MCPMeeting,
    MCPTranscript,
    MCPResponseFormatError,
    build_note,
    build_raw,
    listing_hash,
    parse_mcp_date,
    parse_meetings_detail,
    parse_meetings_listing,
    parse_transcript,
)
from .models import (
    MCP_KEY_PREFIX,
    SOURCE_MCP,
    SOURCE_PUBLIC_API,
    Note,
    is_valid_note_id,
    mcp_archive_key,
    parse_timestamp,
)
from .public_api import GranolaAPIError, NoteNotFoundError, PublicAPIClient
from .render import render_note, render_transcript
from .secure_io import read_json
from .store import RAW_NAME, Archive, content_hash

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .mcp_api import MCPProtocol


# Windows are scanned at a month at a time. `list_meetings` exposes no cursor,
# so a window that comes back at or above SUSPICIOUS_RESULT_COUNT might have
# been truncated by an undocumented cap; it is bisected and rescanned rather
# than trusted. A single day still at the threshold is *reported*, so the
# archive can say "I may be incomplete here" instead of quietly being so.
LISTING_WINDOW_DAYS = 31
FOLDER_WINDOW_DAYS = 180
SUSPICIOUS_RESULT_COUNT = 50
MIN_WINDOW_DAYS = 1

# The MCP exposes no update time, so edits to older meetings are found by
# re-reading a fixed number of archived notes each run: those still missing a
# transcript first, then the least recently checked.
ROLLING_REFRESH_DEFAULT = 30

# A note whose transcript has failed this many times for a reason other than
# throttling -- a meeting with no audio, or a plan that serves no transcripts
# -- stops jumping the refresh queue. Without a limit the queue would never
# converge, spending the scarcest budget on the same notes run after run.
TRANSCRIPT_RETRY_LIMIT = 3

# Sorts before any real timestamp: a note never checked goes first.
_NEVER = datetime.min.replace(tzinfo=UTC)

# Backfill walks backwards until this many consecutive windows come back empty
# -- about six months. Each extra window is one cheap listing call, while
# stopping too early silently drops everything older than the first quiet
# stretch; two windows ended a backfill at any two-month lull. `--since`
# reaches past a longer gap.
EMPTY_WINDOWS_BEFORE_STOP = 6

# Transcripts are the most aggressively limited tool on the MCP, and each
# exhausted retry ladder costs about two minutes of sleeping. Once this many
# notes in a row have been throttled, the quota is spent and the rest of the
# pass would only sleep through the same wall, so the run stops asking. This is
# safe precisely because it is resumable: a note archived without a transcript
# is retried by later syncs, and an archived transcript is never refetched.
TRANSCRIPT_GIVEUP_STREAK = 3


@dataclass(slots=True)
class SyncOptions:
    """Per-run knobs shared by every backend."""

    full: bool = False
    verbose: bool = False
    since: date | None = None
    window_days: int = LISTING_WINDOW_DAYS
    refresh_batch: int = ROLLING_REFRESH_DEFAULT


@dataclass(slots=True)
class SyncCounts:
    """Per-note outcomes tallied across a sync pass."""

    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    detail_fetches: int = 0
    transcript_fetches: int = 0
    list_calls: int = 0
    undated: int = 0
    transcripts_failed: int = 0
    transcripts_deferred: int = 0
    truncated_windows: int = 0
    refresh_failed: int = 0

    def record(self, status: str) -> None:
        """Increment the counter named by a ``SyncResult`` status.

        Args:
            status: One of ``new``, ``updated`` or ``unchanged``.
        """
        setattr(self, status, getattr(self, status) + 1)

    def summary(self) -> str:
        """Render the one-line tally printed at the end of a run.

        Returns:
            A human-readable summary of the pass.
        """
        return (
            f"{self.new} new, {self.updated} updated, "
            f"{self.unchanged} unchanged, {self.skipped} skipped, "
            f"{self.failed} failed ({self.detail_fetches} detail fetches)"
        )

    def warnings(self) -> list[str]:
        """Conditions worth surfacing even on an otherwise successful run.

        Returns:
            Human-readable warnings; empty when the run was clean.
        """
        notes = []
        if self.failed:
            notes.append(
                f"{self.failed} note(s) could not be fetched — the next sync "
                "retries them"
            )
        if self.transcripts_failed:
            notes.append(
                f"{self.transcripts_failed} note(s) archived WITHOUT a transcript "
                "— re-run sync to retry them"
            )
        if self.transcripts_deferred:
            notes.append(
                f"transcripts gave up early: {self.transcripts_deferred} further "
                "note(s) archived without one, unattempted — the server was "
                "throttling every request. Re-run sync to continue where this "
                "pass stopped"
            )
        if self.undated:
            notes.append(
                f"{self.undated} note(s) had an unresolvable date and were filed "
                "under undated/"
            )
        if self.truncated_windows:
            notes.append(
                f"{self.truncated_windows} window(s) may be incomplete — "
                "re-run with --full"
            )
        if self.refresh_failed:
            notes.append(
                f"{self.refresh_failed} older note(s) could not be re-read — "
                "they come round again on a later sync"
            )
        return notes


@dataclass(slots=True)
class _TranscriptLatch:
    """Consecutive throttled transcript fetches, and whether to stop asking.

    Shared by every write pass in a run, so a quota spent in one pass is not
    rediscovered -- at about two minutes a note -- by the next.
    """

    throttled: int = 0
    giving_up: bool = False


def _stub_updated_at(stub: dict[str, Any]) -> str | None:
    """Read a note stub's ``updated_at`` without parsing it.

    Args:
        stub: A note stub from the list endpoint.

    Returns:
        The raw ``updated_at`` string, or ``None`` when absent.
    """
    value = stub.get("updated_at")
    return str(value) if value else None


def sync_public_api(
    archive: Archive,
    client: PublicAPIClient,
    opts: SyncOptions | None = None,
) -> SyncCounts:
    """Fetch new and changed meetings from the public API into the archive.

    Whatever exception escapes, the index is saved first, so the notes this
    pass already wrote are never forgotten and refetched.

    Args:
        archive: The destination archive.
        client: A ready-to-use public API client.
        opts: Per-run options; defaults are an incremental, quiet run.

    Returns:
        The per-note tally for this pass.

    Raises:
        GranolaAPIError: If the API fails unrecoverably.
        KeyboardInterrupt: If the user interrupts.
    """
    opts = opts or SyncOptions()
    index = archive.load_index()
    full = opts.full or not archive.watermark
    updated_after = None if full else archive.watermark

    mode = "full backfill" if full else f"incremental since {updated_after}"
    print(f"Syncing ({mode}) -> {archive.root}")

    counts = SyncCounts()
    seen_ids: set[str] = set()
    high_water: str | None = archive.watermark

    try:
        for stub in client.iter_notes(updated_after=updated_after):
            note_id = str(stub.get("id") or "")
            if not note_id:
                continue
            if not is_valid_note_id(note_id):
                # Ids build filesystem paths and request URLs, so a malformed
                # one is refused rather than sanitized.
                counts.skipped += 1
                print(
                    f"  SKIP  malformed note id from API: {note_id!r}",
                    file=sys.stderr,
                )
                continue
            seen_ids.add(note_id)

            stub_updated = _stub_updated_at(stub)
            if stub_updated and (high_water is None or stub_updated > high_water):
                high_water = stub_updated

            # Skip the detail call entirely when the stub proves nothing has
            # changed. This is what makes a no-op re-sync cheap.
            #
            # The two timestamps are compared as instants, not strings: the
            # index holds `datetime.isoformat` output ("...775000+00:00") while
            # the API sends "...775Z". Comparing the raw strings never matches,
            # which silently defeats this skip on every note.
            entry = index.get(note_id)
            stub_instant = parse_timestamp(stub_updated)
            if (
                entry
                and stub_instant is not None
                and parse_timestamp(entry.get("updated_at")) == stub_instant
                and (archive.root / str(entry.get("path", ""))).is_dir()
            ):
                counts.unchanged += 1
                continue

            try:
                payload = client.get_note(note_id, include_transcript=True)
                counts.detail_fetches += 1
            except NoteNotFoundError:
                # Still processing, or has no summary/transcript yet.
                counts.skipped += 1
                if opts.verbose:
                    print(f"  skip  {note_id} — no summary/transcript yet")
                continue
            except GranolaAPIError as exc:
                counts.failed += 1
                print(f"  FAIL  {note_id} — {exc}", file=sys.stderr)
                continue

            note = Note.from_api(payload)
            if not note.id:
                note.id = note_id

            if archive.is_unchanged(note.id, content_hash(note.raw)):
                counts.unchanged += 1
                continue

            # If this meeting was previously archived through MCP, take the
            # entry over rather than writing a second copy under the `not_*`
            # key. The higher-fidelity source always wins.
            retired = archive.adopt_mcp_entry(note)
            if retired and opts.verbose:
                print(f"  adopt     {note.display_title} (was {retired})")

            transcript_md = render_transcript(note)
            note_md = render_note(note, has_transcript_file=bool(transcript_md))
            result = archive.write_note(note, note_md, transcript_md)
            counts.record(result.status)
            if opts.verbose:
                print(f"  {result.status:9} {note.display_title}")
    except BaseException:
        # Not only API errors: anything unexpected would otherwise leave
        # notes on disk that the index has never heard of.
        archive.save_index()
        raise

    if full:
        # Scoped to this source: the MCP backend sees a different id namespace,
        # so an unscoped sweep would flag every note the other backend owns.
        newly_missing = archive.mark_upstream_missing(
            seen_ids, source=SOURCE_PUBLIC_API
        )
        if newly_missing:
            print(
                f"  {len(newly_missing)} archived note(s) no longer upstream "
                "— flagged, not deleted."
            )

    archive.save_index()
    if high_water and counts.failed == 0:
        archive.save_source_state(SOURCE_PUBLIC_API, updated_after=high_water)

    return counts


# -- MCP backend -----------------------------------------------------------


def _iter_windows(
    start: date, end: date, days: int
) -> Iterator[tuple[date, date]]:
    """Yield inclusive date windows covering a range, newest first.

    Args:
        start: Inclusive first date.
        end: Inclusive last date.
        days: Window width.

    Yields:
        ``(window_start, window_end)`` pairs.
    """
    cursor = end
    step = timedelta(days=max(1, days) - 1)
    while cursor >= start:
        window_start = max(start, cursor - step)
        yield window_start, cursor
        cursor = window_start - timedelta(days=1)


def _scan_window(
    client: MCPProtocol,
    start: date,
    end: date,
    counts: SyncCounts,
    *,
    folder_id: str | None = None,
    verbose: bool = False,
) -> list[MCPMeeting]:
    """List one window, bisecting when the result set looks truncated.

    Args:
        client: The MCP backend.
        start: Inclusive window start.
        end: Inclusive window end.
        counts: Tally to update.
        folder_id: Restrict to one folder.
        verbose: Whether to narrate the bisection.

    Returns:
        The meetings found, deduplicated by id.
    """
    counts.list_calls += 1
    text = client.list_meetings(
        custom_start=start, custom_end=end, folder_id=folder_id
    )
    _, meetings = parse_meetings_listing(text)

    if len(meetings) < SUSPICIOUS_RESULT_COUNT:
        return meetings

    span = (end - start).days + 1
    if span <= MIN_WINDOW_DAYS:
        # A single day at the cap: we cannot subdivide further, so record it
        # rather than silently returning a possibly-partial list.
        counts.truncated_windows += 1
        print(
            f"  WARN  {start} returned {len(meetings)} results at the "
            "subdivision limit — this day may be incomplete",
            file=sys.stderr,
        )
        return meetings

    if verbose:
        print(f"  bisect    {start}..{end} ({len(meetings)} results)")
    midpoint = start + timedelta(days=span // 2)
    left = _scan_window(
        client, start, midpoint - timedelta(days=1), counts,
        folder_id=folder_id, verbose=verbose,
    )
    right = _scan_window(
        client, midpoint, end, counts, folder_id=folder_id, verbose=verbose
    )

    merged: dict[str, MCPMeeting] = {}
    for meeting in [*left, *right]:
        merged[meeting.meeting_id] = meeting
    return list(merged.values())


def _folder_membership(
    client: MCPProtocol, start: date, end: date, counts: SyncCounts, verbose: bool
) -> dict[str, set[str]]:
    """Map meeting id to folder names.

    Neither ``list_meetings`` nor ``get_meetings`` returns folder membership,
    so the only way to recover it is one listing per folder. Folder *names* are
    the join key across backends: MCP folder ids are UUIDs in a different
    namespace from the public API's ``fol_*``.

    Args:
        client: The MCP backend.
        start: Inclusive scan start.
        end: Inclusive scan end.
        counts: Tally to update.
        verbose: Whether to narrate.

    Returns:
        A mapping of meeting id to the folder names containing it.
    """
    membership: dict[str, set[str]] = {}
    for folder in client.list_folders():
        folder_id = str(folder.get("id") or "")
        name = str(folder.get("title") or folder.get("name") or "").strip()
        if not folder_id or not name:
            continue
        for window_start, window_end in _iter_windows(start, end, FOLDER_WINDOW_DAYS):
            for meeting in _scan_window(
                client, window_start, window_end, counts,
                folder_id=folder_id, verbose=verbose,
            ):
                membership.setdefault(meeting.meeting_id, set()).add(name)
    return membership


def _discover(
    client: MCPProtocol,
    counts: SyncCounts,
    *,
    today: date,
    floor: date | None,
    window_days: int,
    verbose: bool,
) -> tuple[dict[str, MCPMeeting], date]:
    """Walk backwards until the history runs out.

    Args:
        client: The MCP backend.
        counts: Tally to update.
        today: The most recent date to scan.
        floor: Stop here; ``None`` means keep going until the history is dry.
        window_days: Window width.
        verbose: Whether to narrate.

    Returns:
        The meetings found, keyed by id, and the earliest date scanned.
    """
    found: dict[str, MCPMeeting] = {}
    cursor = today
    empty_runs = 0
    earliest = today
    step = timedelta(days=max(1, window_days) - 1)

    while True:
        window_start = cursor - step
        if floor is not None and window_start < floor:
            window_start = floor
        meetings = _scan_window(
            client, window_start, cursor, counts, verbose=verbose
        )
        for meeting in meetings:
            found[meeting.meeting_id] = meeting
        earliest = window_start

        empty_runs = empty_runs + 1 if not meetings else 0
        if floor is not None and window_start <= floor:
            break
        if floor is None and empty_runs >= EMPTY_WINDOWS_BEFORE_STOP:
            break
        cursor = window_start - timedelta(days=1)

    return found, earliest


def sync_mcp(
    archive: Archive,
    client: MCPProtocol,
    opts: SyncOptions | None = None,
    *,
    today: date | None = None,
    server_url: str = "",
) -> SyncCounts:
    """Fetch meetings from the Granola MCP into the archive.

    The MCP exposes neither an updated-since filter nor a cursor, so change
    detection is rebuilt from what the listing tool does return:

    1. Date windows stand in for the public API's watermark.
    2. ``listing_hash`` over the verbatim ``<meeting>`` element stands in for
       the stub's ``updated_at``, gating the expensive detail call.
    3. The existing content hash still gates the disk write.

    Older notes are revisited by a rolling refresh: each run also re-reads
    up to ``refresh_batch`` archived notes, missing transcripts first, by
    re-listing the weeks they fall in. A summary regenerated long after its
    meeting is therefore caught when the rotation reaches it rather than
    immediately. That is a real regression against the public API, and it is
    documented rather than hidden.

    Args:
        archive: The destination archive.
        client: A ready-to-use MCP backend.
        opts: Per-run options.
        today: The date to scan back from; injectable for tests.
        server_url: Recorded in ``raw.json`` for provenance.

    Returns:
        The per-note tally for this pass.

    Raises:
        MCPResponseFormatError: If a response no longer has the expected
            shape. Drift aborts the pass, but anything already written is
            indexed first.
    """
    opts = opts or SyncOptions()
    today = today or datetime.now().astimezone().date()
    counts = SyncCounts()

    state = archive.source_state(SOURCE_MCP)
    index = archive.load_index()
    uuid_map = archive.uuid_index()

    full = opts.full or not state.get("scanned_through")
    trailing_start = today - timedelta(days=opts.window_days)
    # Meetings listed only because the last run failed to fetch them.
    retry_ids: set[str] = set()

    if full:
        mode = "full backfill" if not opts.since else f"backfill since {opts.since}"
        print(f"Syncing MCP ({mode}) -> {archive.root}")
        # Always month-wide: `--window` sizes the trailing rescan, and a small
        # one here would let a couple of short empty windows end the backfill.
        meetings, earliest = _discover(
            client, counts, today=today, floor=opts.since,
            window_days=LISTING_WINDOW_DAYS, verbose=opts.verbose,
        )
    else:
        print(f"Syncing MCP (trailing {opts.window_days} days) -> {archive.root}")
        meetings = {
            m.meeting_id: m
            for m in _scan_window(
                client, trailing_start, today, counts, verbose=opts.verbose
            )
        }
        earliest = str(state.get("earliest_scanned") or trailing_start.isoformat())
        earliest = date.fromisoformat(earliest)

        # The trailing window alone would never list a failure older than it.
        retry_from = _stored_date(state.get("retry_from"))
        if retry_from is not None and retry_from < trailing_start:
            print(f"  retrying what failed last time, back to {retry_from}")
            for window_start, window_end in _iter_windows(
                retry_from, trailing_start - timedelta(days=1), LISTING_WINDOW_DAYS
            ):
                for meeting in _scan_window(
                    client, window_start, window_end, counts, verbose=opts.verbose
                ):
                    meetings[meeting.meeting_id] = meeting
                    retry_ids.add(meeting.meeting_id)

    folders = (
        _folder_membership(client, earliest, today, counts, opts.verbose)
        if full
        else {}
    )

    pending, settled = _triage(
        archive, index, uuid_map, meetings.values(), trailing_start, counts
    )
    queued = {m.meeting_id for m in pending}

    # Re-read a bounded number of archived notes as well, so edits and
    # throttled transcripts older than the trailing window are still picked
    # up. Each is stamped now, whether or not it can be reached.
    candidates = _old_note_candidates(index, queued, opts.refresh_batch)
    stamp = _now_iso()
    for key in candidates:
        _mcp_block(index[key])["refresh_attempted_at"] = stamp
    wanted = {key.removeprefix(MCP_KEY_PREFIX) for key in candidates}

    unlisted_days = [
        created.date()
        for key in candidates
        if key.removeprefix(MCP_KEY_PREFIX) not in meetings
        and (created := parse_timestamp(index[key].get("created_at"))) is not None
    ]
    if not full and unlisted_days:
        # Re-list the weeks they fall in rather than asking get_meetings for
        # ids nobody listed: how it answers for a deleted meeting is unknown,
        # and a meeting that is no longer listed is simply left alone.
        fresh: list[MCPMeeting] = []
        for window_start, window_end in _relist_windows(
            unlisted_days, LISTING_WINDOW_DAYS
        ):
            for meeting in _scan_window(
                client, window_start, window_end, counts, verbose=opts.verbose
            ):
                if meeting.meeting_id not in meetings:
                    meetings[meeting.meeting_id] = meeting
                    fresh.append(meeting)
        # Neighbors listed alongside get ordinary change detection, which
        # also catches a retitle of an old meeting.
        more, also = _triage(
            archive, index, uuid_map,
            [m for m in fresh if m.meeting_id not in wanted],
            trailing_start, counts,
        )
        pending += more
        settled |= also
        queued |= {m.meeting_id for m in more}

    refresh = [
        meetings[uuid]
        for uuid in (key.removeprefix(MCP_KEY_PREFIX) for key in candidates)
        if uuid in meetings and uuid not in queued
    ]
    counts.unchanged += len(settled - {m.meeting_id for m in refresh})

    # A meeting that failed last time is fetched on its own, so one that
    # always fails cannot take a batch of good ones down with it on every run.
    retries = [m for m in pending if m.meeting_id in retry_ids]
    pending = [m for m in pending if m.meeting_id not in retry_ids]

    latch = _TranscriptLatch()
    try:
        failed = _write_mcp_meetings(
            archive, client, pending, folders, counts, opts, server_url,
            latch=latch,
        )
        failed += _write_mcp_meetings(
            archive, client, retries, folders, counts, opts, server_url,
            latch=latch, batch_size=1,
        )
        # Last, so real work gets the transcript budget first; optional, so a
        # note that cannot be re-read warns instead of failing the run.
        _write_mcp_meetings(
            archive, client, refresh, folders, counts, opts, server_url,
            latch=latch, optional=True,
        )
    except BaseException:
        # Drift is still loud -- the error propagates -- but the notes this
        # pass already wrote are on disk, and an index that forgot them would
        # refetch every one, rate-limited transcripts included.
        archive.save_index()
        raise

    retry_floor = _retry_floor(failed, earliest)
    archive.save_index()
    archive.save_source_state(
        SOURCE_MCP,
        earliest_scanned=earliest.isoformat(),
        scanned_through=today.isoformat(),
        parser_version=PARSER_VERSION,
        truncated_windows=counts.truncated_windows,
        retry_from=retry_floor.isoformat() if retry_floor else None,
        **({"last_full_scan": today.isoformat()} if full else {}),
    )
    return counts


def _is_throttling(exc: BaseException) -> bool:
    """Check whether a failed transcript fetch was the server throttling us.

    Args:
        exc: The exception raised by the fetch.

    Returns:
        ``True`` when the fetch failed because every retry stayed rate limited.
        Falls back to matching the message, so a backend that raises its own
        error type -- a test fake, or a future non-SDK client -- still trips the
        latch instead of grinding through the whole pass.
    """
    from .mcp_api import MCPRateLimitError

    if isinstance(exc, MCPRateLimitError):
        return True
    text = str(exc).lower()
    return "rate limit" in text or "too many requests" in text


def _within(meeting: MCPMeeting, trailing_start: date) -> bool:
    """Check whether a meeting falls inside the trailing rescan window.

    Args:
        meeting: The listed meeting.
        trailing_start: The first date of the trailing window.

    Returns:
        ``True`` when the meeting is recent enough to always re-read. An
        unparseable date is treated as recent, so it is re-read rather than
        skipped on a stale hash.
    """
    parsed = parse_mcp_date(meeting.date_text)
    if parsed.instant is None:
        return True
    return parsed.instant.date() >= trailing_start


def _triage(
    archive: Archive,
    index: dict[str, dict[str, Any]],
    uuid_map: dict[str, list[str]],
    meetings: Iterable[MCPMeeting],
    trailing_start: date,
    counts: SyncCounts,
) -> tuple[list[MCPMeeting], set[str]]:
    """Split listed meetings into those needing a detail call and the rest.

    Args:
        archive: The archive being synced.
        index: The loaded index.
        uuid_map: UUID to archive keys, for the never-downgrade check.
        meetings: Listed meetings.
        trailing_start: First day of the always-re-read trailing window.
        counts: Tally to update.

    Returns:
        The meetings to fetch, and the ids settled without a fetch. The
        caller counts the settled ones as unchanged, because it knows which
        of them a refresh is about to re-read anyway.
    """
    pending: list[MCPMeeting] = []
    settled: set[str] = set()
    for meeting in meetings:
        key = mcp_archive_key(meeting.meeting_id)
        if key is None:
            counts.skipped += 1
            print(
                f"  SKIP  malformed meeting id from MCP: {meeting.meeting_id!r}",
                file=sys.stderr,
            )
            continue

        # Never downgrade: a note already archived from the public API is
        # higher fidelity than anything the MCP can produce.
        owners = uuid_map.get(meeting.meeting_id, [])
        if any(not owner.startswith(MCP_KEY_PREFIX) for owner in owners):
            settled.add(meeting.meeting_id)
            continue

        entry = index.get(key) or {}
        stored = (entry.get("mcp") or {}).get("listing_hash")
        if (
            stored == listing_hash(meeting.element_text)
            and not _within(meeting, trailing_start)
            and (archive.root / str(entry.get("path", ""))).is_dir()
        ):
            settled.add(meeting.meeting_id)
            continue
        pending.append(meeting)
    return pending, settled


def _mcp_block(entry: dict[str, Any]) -> dict[str, Any]:
    """The ``mcp`` bookkeeping block of an index entry, created if absent.

    Args:
        entry: A live index entry.

    Returns:
        The block, owned by the entry. ``index.json`` is on disk and can be
        edited, so a value that is not a mapping is replaced, not trusted.
    """
    block = entry.get("mcp")
    if not isinstance(block, dict):
        block = entry["mcp"] = {}
    return block


def _latest(*stamps: Any) -> datetime:
    """The most recent of some stored timestamps, compared as instants.

    Stamps carry the local offset, so comparing the strings would misorder
    two taken either side of a daylight-saving change.

    Args:
        *stamps: ISO 8601 strings, or ``None``.

    Returns:
        The latest instant, or :data:`_NEVER` when none parses.
    """
    instants = [t for t in map(parse_timestamp, stamps) if t is not None]
    return max(instants, default=_NEVER)


def _old_note_candidates(
    index: dict[str, dict[str, Any]], queued: set[str], limit: int
) -> list[str]:
    """Pick the archived MCP notes this run should re-read.

    Nothing in the protocol announces an edit, and a plain sync lists only
    the trailing window, so a fixed number of archived notes are re-read each
    run, drawn from the whole archive. Notes still missing a transcript come
    first -- a throttled backfill leaves many -- then the least recently
    checked, so edits to old meetings are found in rotation.

    Args:
        index: The loaded index.
        queued: Meeting ids this run already reads.
        limit: How many to pick.

    Returns:
        Archive keys, most deserving first.
    """
    if limit <= 0:
        return []
    ranked: list[tuple[int, datetime, str]] = []
    for key, entry in index.items():
        if not key.startswith(MCP_KEY_PREFIX) or entry.get("upstream_missing"):
            continue
        if key.removeprefix(MCP_KEY_PREFIX) in queued:
            continue
        block = entry.get("mcp") if isinstance(entry.get("mcp"), dict) else {}
        failures = block.get("transcript_failures")
        failures = failures if isinstance(failures, int) else 0
        missing = (
            not block.get("transcript_fetched_at")
            and failures < TRANSCRIPT_RETRY_LIMIT
        )
        # Every candidate is stamped when picked, so one that could not be
        # reached -- gone upstream, say -- rotates out instead of holding the
        # front of the queue.
        checked = _latest(
            block.get("refresh_attempted_at"),
            block.get("transcript_attempted_at" if missing else "detail_fetched_at"),
        )
        ranked.append((0 if missing else 1, checked, key))
    ranked.sort()
    return [key for _, _, key in ranked[:limit]]


def _relist_windows(days: list[date], width: int) -> list[tuple[date, date]]:
    """Cover some meeting dates with as few listing windows as possible.

    Candidates are picked in check order, which tracks meeting order, so a
    batch usually spans a few weeks and costs one or two listing calls.

    Args:
        days: The UTC dates of the meetings to find.
        width: The widest window to list at once.

    Returns:
        Inclusive ``(start, end)`` windows, oldest first.
    """
    windows: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        # A day either side: listings are keyed by local date, instants by UTC.
        start, end = day - timedelta(days=1), day + timedelta(days=1)
        if windows and (end - windows[-1][0]).days < width:
            windows[-1] = (windows[-1][0], end)
        else:
            windows.append((start, end))
    return windows


def _chunk(items: list[MCPMeeting], size: int) -> Iterator[list[MCPMeeting]]:
    """Split a list into fixed-size batches.

    Args:
        items: Items to batch.
        size: Maximum batch size.

    Yields:
        Batches of at most ``size`` items.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _write_mcp_meetings(
    archive: Archive,
    client: MCPProtocol,
    pending: list[MCPMeeting],
    folders: dict[str, set[str]],
    counts: SyncCounts,
    opts: SyncOptions,
    server_url: str,
    *,
    latch: _TranscriptLatch,
    batch_size: int | None = None,
    optional: bool = False,
) -> list[MCPMeeting]:
    """Fetch details and transcripts for queued meetings, then archive them.

    Args:
        archive: The destination archive.
        client: The MCP backend.
        pending: Meetings needing a detail call.
        folders: Meeting id to folder names.
        counts: Tally to update.
        opts: Per-run options.
        server_url: Recorded in ``raw.json``.
        latch: Throttling state shared with the run's other write passes.
        batch_size: Ids per ``get_meetings`` call; defaults to the server's
            maximum.
        optional: Whether these are re-reads of notes already archived. A
            failed optional batch loses nothing, so it warns and is counted
            as ``refresh_failed`` rather than failing the run.

    Returns:
        The meetings whose detail fetch failed and must be retried. An
        optional pass never returns any.
    """
    from .mcp_api import MAX_MEETINGS_PER_CALL

    index = archive.load_index()
    listings = {m.meeting_id: m for m in pending}
    failed: list[MCPMeeting] = []

    for batch in _chunk(pending, batch_size or MAX_MEETINGS_PER_CALL):
        try:
            text = client.get_meetings([m.meeting_id for m in batch])
            counts.detail_fetches += len(batch)
            _, details = parse_meetings_detail(text)
        except MCPResponseFormatError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad batch must not end the run
            if optional:
                counts.refresh_failed += len(batch)
                print(
                    f"  WARN  could not re-read {len(batch)} older note(s) — {exc}",
                    file=sys.stderr,
                )
            else:
                counts.failed += len(batch)
                failed.extend(batch)
                print(f"  FAIL  batch of {len(batch)} — {exc}", file=sys.stderr)
            continue

        for detail in details:
            key = mcp_archive_key(detail.meeting_id)
            if key is None:
                counts.skipped += 1
                continue

            entry = index.get(key) or {}
            previous = entry.get("mcp") if isinstance(entry.get("mcp"), dict) else {}
            transcript_at = previous.get("transcript_fetched_at")
            attempted_at = previous.get("transcript_attempted_at")
            failures = previous.get("transcript_failures")
            failures = failures if isinstance(failures, int) else 0

            # A transcript already fetched -- even an empty one -- is
            # immutable in practice, and re-fetching risks replacing good
            # content with a degraded re-render while spending the scarcest
            # budget there is. It is reloaded from raw.json rather than skipped
            # outright: dropping it would change the content hash on every
            # run, so the note would look updated forever.
            transcript = (
                _archived_transcript(archive, entry) if transcript_at else None
            )
            if transcript is None and latch.giving_up:
                # The quota is spent. Archive the note now rather than sleeping
                # through a retry ladder that cannot succeed; a later run
                # picks these notes up again.
                counts.transcripts_deferred += 1
            elif transcript is None:
                attempted_at = _now_iso()
                try:
                    payload = client.get_meeting_transcript(detail.meeting_id)
                    transcript = parse_transcript(payload)
                    if transcript.meeting_id != detail.meeting_id:
                        # Never file one meeting's words under another's.
                        raise ValueError(
                            f"asked for {detail.meeting_id}, got a transcript "
                            f"for {transcript.meeting_id}"
                        )
                    counts.transcript_fetches += 1
                    transcript_at = attempted_at
                    failures = 0
                    latch.throttled = 0
                except MCPResponseFormatError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    transcript = None
                    # Never silent. A transcript is the most valuable thing in
                    # the archive; losing one quietly is the worst outcome
                    # here, and swallowing this is how a live backfill once
                    # produced 66 notes with zero transcripts.
                    counts.transcripts_failed += 1
                    print(
                        f"  WARN  no transcript for {key} — {exc}",
                        file=sys.stderr,
                    )
                    # Only throttling says anything about the *next* note; one
                    # unparseable or missing transcript says nothing at all.
                    # Throttling is also the one failure that says nothing
                    # about *this* note, so it never counts against it.
                    throttling = _is_throttling(exc)
                    if not throttling:
                        failures += 1
                    latch.throttled = latch.throttled + 1 if throttling else 0
                    if latch.throttled >= TRANSCRIPT_GIVEUP_STREAK:
                        latch.giving_up = True
                        print(
                            f"  STOP  {latch.throttled} transcripts throttled in a row "
                            "— skipping the rest this pass; re-run sync to "
                            "retry them",
                            file=sys.stderr,
                        )

            names = folders.get(detail.meeting_id) or set(previous.get("folders") or [])
            listing = listings.get(detail.meeting_id, detail)
            raw = build_raw(
                detail,
                listing.element_text,
                transcript,
                sorted(names),
                server_url,
            )
            note = build_note(
                detail, raw, transcript=transcript, folder_names=sorted(names)
            )
            if note.created_at is None:
                counts.undated += 1

            bookkeeping = {
                "parser_version": PARSER_VERSION,
                "listing_hash": listing_hash(listing.element_text),
                "detail_fetched_at": _now_iso(),
                "transcript_fetched_at": transcript_at,
                "transcript_attempted_at": attempted_at,
                "transcript_failures": failures,
                "date_text": detail.date_text,
                "tz_resolved": parse_mcp_date(detail.date_text).tz_resolved,
                "folders": sorted(names),
            }

            # Hash only the verbatim tool output. Hashing the wrapper would let
            # a parser-version bump rewrite every directory in the archive.
            digest = content_hash(raw["mcp"])
            if archive.is_unchanged(key, digest):
                # Record the check anyway: without it the rolling refresh kept
                # picking the same unchanged notes on every run.
                entry["mcp"] = {**previous, **bookkeeping}
                counts.unchanged += 1
                continue

            transcript_md = render_transcript(note)
            note_md = render_note(note, has_transcript_file=bool(transcript_md))
            result = archive.write_note(
                note,
                note_md,
                transcript_md,
                source=SOURCE_MCP,
                digest=digest,
                extra={"mcp": bookkeeping},
            )
            counts.record(result.status)
            if opts.verbose:
                print(f"  {result.status:9} {note.display_title}")

    return failed


def _stored_date(value: Any) -> date | None:
    """Read an ISO date back out of the sync state.

    Args:
        value: Whatever the state file holds.

    Returns:
        The date, or ``None`` when absent or unreadable -- the state file is
        on disk and can be edited, so it is read defensively.
    """
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _retry_floor(failed: list[MCPMeeting], fallback: date) -> date | None:
    """The date the next run must list back to, to retry this run's failures.

    This is the MCP's version of the public API holding its watermark. With
    no updated-since filter, a failed meeting outside the trailing window is
    never listed again by an incremental run, so the next run is told how far
    back to look.

    Args:
        failed: Meetings whose detail fetch failed.
        fallback: The date to use for a failure whose own date won't parse.

    Returns:
        A day before the earliest failure -- listings are keyed by local date
        and instants by UTC -- or ``None`` when nothing failed.
    """
    if not failed:
        return None
    dates = []
    for meeting in failed:
        instant = parse_mcp_date(meeting.date_text).instant
        dates.append(instant.date() if instant else fallback)
    return min(dates) - timedelta(days=1)


def _now_iso() -> str:
    """The current local time as an ISO 8601 string.

    Returns:
        The timestamp, for index bookkeeping only -- never for ``raw.json``,
        where it would change the content hash on every run.
    """
    return datetime.now().astimezone().isoformat()


def _archived_transcript(archive: Archive, entry: dict[str, Any]) -> MCPTranscript | None:
    """Reload a previously archived transcript from ``raw.json``.

    Args:
        archive: The archive holding the note.
        entry: That note's index entry.

    Returns:
        The transcript, or ``None`` when it cannot be recovered -- in which
        case the caller refetches rather than silently dropping it. An empty
        transcript comes back empty, not ``None``: it was fetched, and asking
        again would spend the scarcest budget on the same silence.
    """
    path = entry.get("path")
    if not path:
        return None
    try:
        raw = read_json(archive.root / str(path) / RAW_NAME, default={})
    except OSError:
        return None
    if not isinstance(raw, dict):
        return None
    block = (raw.get("mcp") or {}).get("get_meeting_transcript")
    if not isinstance(block, dict):
        return None
    return MCPTranscript(
        meeting_id=str(block.get("id") or ""),
        title=str(block.get("title") or ""),
        text=str(block.get("transcript") or ""),
    )


def scan_mcp_meeting_ids(
    client: MCPProtocol,
    start: date,
    end: date,
    *,
    window_days: int = LISTING_WINDOW_DAYS,
) -> tuple[set[str], int]:
    """Collect every meeting id the MCP reports across a date range.

    Backend traversal lives here rather than in the CLI, so ``verify`` gets the
    same bisect-on-suspicion scan ``sync_mcp`` uses -- a reconcile that silently
    read a truncated listing would report a clean archive that is not.

    Args:
        client: The MCP backend.
        start: Inclusive first date.
        end: Inclusive last date.
        window_days: Window width.

    Returns:
        The meeting ids seen, and the number of listing calls spent so the
        caller can report its own cost.
    """
    counts = SyncCounts()
    seen: set[str] = set()
    for window_start, window_end in _iter_windows(start, end, window_days):
        for meeting in _scan_window(client, window_start, window_end, counts):
            seen.add(meeting.meeting_id)
    return seen, counts.list_calls
