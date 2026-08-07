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
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
# re-reading a fixed number of the least-recently-checked notes each run.
ROLLING_REFRESH_DEFAULT = 30

# Backfill walks backwards until this many consecutive windows come back empty.
EMPTY_WINDOWS_BEFORE_STOP = 2


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
    truncated_windows: int = 0

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
        if self.transcripts_failed:
            notes.append(
                f"{self.transcripts_failed} note(s) archived WITHOUT a transcript "
                "— re-run sync to retry them"
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
        return notes


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

    Args:
        archive: The destination archive.
        client: A ready-to-use public API client.
        opts: Per-run options; defaults are an incremental, quiet run.

    Returns:
        The per-note tally for this pass.

    Raises:
        GranolaAPIError: If the API fails unrecoverably. The index is saved
            first, so partial progress survives.
        KeyboardInterrupt: If the user interrupts. The index is saved first.
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
                # one is refused rather than sanitised.
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
    except GranolaAPIError:
        archive.save_index()
        raise
    except KeyboardInterrupt:
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

    A summary regenerated long after its meeting is caught by the rolling
    refresh rather than immediately. That is a real regression against the
    public API, and it is documented rather than hidden.

    Args:
        archive: The destination archive.
        client: A ready-to-use MCP backend.
        opts: Per-run options.
        today: The date to scan back from; injectable for tests.
        server_url: Recorded in ``raw.json`` for provenance.

    Returns:
        The per-note tally for this pass.
    """
    opts = opts or SyncOptions()
    today = today or datetime.now().astimezone().date()
    counts = SyncCounts()

    state = archive.source_state(SOURCE_MCP)
    index = archive.load_index()
    uuid_map = archive.uuid_index()

    full = opts.full or not state.get("scanned_through")
    trailing_start = today - timedelta(days=opts.window_days)

    if full:
        mode = "full backfill" if not opts.since else f"backfill since {opts.since}"
        print(f"Syncing MCP ({mode}) -> {archive.root}")
        meetings, earliest = _discover(
            client, counts, today=today, floor=opts.since,
            window_days=opts.window_days, verbose=opts.verbose,
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

    folders = (
        _folder_membership(client, earliest, today, counts, opts.verbose)
        if full
        else {}
    )

    # Decide which meetings need a detail call.
    pending: list[MCPMeeting] = []
    for meeting in meetings.values():
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
        if any(not owner.startswith("mcp_") for owner in owners):
            counts.unchanged += 1
            continue

        entry = index.get(key) or {}
        stored = (entry.get("mcp") or {}).get("listing_hash")
        in_trailing = _within(meeting, trailing_start)
        if (
            stored == listing_hash(meeting.element_text)
            and not in_trailing
            and (archive.root / str(entry.get("path", ""))).is_dir()
        ):
            counts.unchanged += 1
            continue
        pending.append(meeting)

    pending.extend(_refresh_batch(archive, index, meetings, opts.refresh_batch))

    _write_mcp_meetings(
        archive, client, pending, folders, counts, opts, server_url
    )

    archive.save_index()
    archive.save_source_state(
        SOURCE_MCP,
        earliest_scanned=earliest.isoformat(),
        scanned_through=today.isoformat(),
        parser_version=PARSER_VERSION,
        truncated_windows=counts.truncated_windows,
        **({"last_full_scan": today.isoformat()} if full else {}),
    )
    return counts


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


def _refresh_batch(
    archive: Archive,
    index: dict[str, dict[str, Any]],
    seen: dict[str, MCPMeeting],
    limit: int,
) -> list[MCPMeeting]:
    """Pick the least-recently-checked MCP notes for a re-read.

    Nothing in the protocol announces an edit, so this amortises the cost of
    finding one: a fixed number of notes are re-read each run.

    Args:
        archive: The archive being synced.
        index: The loaded index.
        seen: Meetings already queued this run.
        limit: How many notes to refresh.

    Returns:
        Meetings to re-read. Only notes seen in this run's listing are
        eligible, since a detail call needs a listing element to pair with.
    """
    if limit <= 0:
        return []
    candidates = [
        (str((entry.get("mcp") or {}).get("detail_fetched_at") or ""), key)
        for key, entry in index.items()
        if key.startswith("mcp_") and not entry.get("upstream_missing")
    ]
    candidates.sort()
    picked: list[MCPMeeting] = []
    for _, key in candidates:
        if len(picked) >= limit:
            break
        meeting = seen.get(key.removeprefix("mcp_"))
        if meeting is not None and meeting not in picked:
            picked.append(meeting)
    return picked


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
) -> None:
    """Fetch details and transcripts for queued meetings, then archive them.

    Args:
        archive: The destination archive.
        client: The MCP backend.
        pending: Meetings needing a detail call.
        folders: Meeting id to folder names.
        counts: Tally to update.
        opts: Per-run options.
        server_url: Recorded in ``raw.json``.
    """
    from .mcp_api import MAX_MEETINGS_PER_CALL

    index = archive.load_index()
    listings = {m.meeting_id: m for m in pending}

    for batch in _chunk(pending, MAX_MEETINGS_PER_CALL):
        try:
            text = client.get_meetings([m.meeting_id for m in batch])
            counts.detail_fetches += len(batch)
            _, details = parse_meetings_detail(text)
        except MCPResponseFormatError:
            raise
        except Exception as exc:  # noqa: BLE001 - one bad batch must not end the run
            counts.failed += len(batch)
            print(f"  FAIL  batch of {len(batch)} — {exc}", file=sys.stderr)
            continue

        for detail in details:
            key = mcp_archive_key(detail.meeting_id)
            if key is None:
                counts.skipped += 1
                continue

            entry = index.get(key) or {}
            previous = entry.get("mcp") or {}
            transcript_at = previous.get("transcript_fetched_at")

            # A transcript already archived is immutable in practice, and
            # re-fetching risks replacing good content with a degraded
            # re-render. It is reloaded from raw.json rather than skipped
            # outright: dropping it would change the content hash on every
            # run, so the note would look updated forever and would re-render
            # without its transcript.
            transcript = (
                _archived_transcript(archive, entry)
                if entry.get("has_transcript") and transcript_at
                else None
            )
            if transcript is None:
                try:
                    payload = client.get_meeting_transcript(detail.meeting_id)
                    transcript = parse_transcript(payload)
                    counts.transcript_fetches += 1
                    transcript_at = _now_iso()
                except MCPResponseFormatError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    # Never silent. A transcript is the most valuable thing in
                    # the archive; losing one quietly is the worst outcome
                    # here, and swallowing this is how a live backfill once
                    # produced 66 notes with zero transcripts.
                    counts.transcripts_failed += 1
                    print(
                        f"  WARN  no transcript for {key} — {exc}",
                        file=sys.stderr,
                    )

            names = folders.get(detail.meeting_id) or set(previous.get("folders") or [])
            raw = build_raw(
                detail,
                listings.get(detail.meeting_id, detail).element_text,
                transcript,
                sorted(names),
                server_url,
            )
            note = build_note(
                detail, raw, transcript=transcript, folder_names=sorted(names)
            )
            if note.created_at is None:
                counts.undated += 1

            # Hash only the verbatim tool output. Hashing the wrapper would let
            # a parser-version bump rewrite every directory in the archive.
            digest = content_hash(raw["mcp"])
            if archive.is_unchanged(key, digest):
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
                extra={
                    "mcp": {
                        "parser_version": PARSER_VERSION,
                        "listing_hash": listing_hash(
                            listings.get(detail.meeting_id, detail).element_text
                        ),
                        "detail_fetched_at": _now_iso(),
                        "transcript_fetched_at": transcript_at,
                        "date_text": detail.date_text,
                        "tz_resolved": parse_mcp_date(detail.date_text).tz_resolved,
                        "folders": sorted(names),
                    }
                },
            )
            counts.record(result.status)
            if opts.verbose:
                print(f"  {result.status:9} {note.display_title}")


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
        case the caller refetches rather than silently dropping it.
    """
    path = entry.get("path")
    if not path:
        return None
    try:
        raw = read_json(archive.root / str(path) / RAW_NAME, default={})
    except OSError:
        return None
    block = (raw.get("mcp") or {}).get("get_meeting_transcript")
    if not isinstance(block, dict) or not block.get("transcript"):
        return None
    return MCPTranscript(
        meeting_id=str(block.get("id") or ""),
        title=str(block.get("title") or ""),
        text=str(block.get("transcript") or ""),
    )
