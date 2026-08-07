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
from dataclasses import dataclass
from typing import Any

from .models import Note, is_valid_note_id, parse_timestamp
from .public_api import GranolaAPIError, NoteNotFoundError, PublicAPIClient
from .render import render_note, render_transcript
from .store import Archive, content_hash


@dataclass(slots=True)
class SyncOptions:
    """Per-run knobs shared by every backend."""

    full: bool = False
    verbose: bool = False


@dataclass(slots=True)
class SyncCounts:
    """Per-note outcomes tallied across a sync pass."""

    new: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    failed: int = 0
    detail_fetches: int = 0

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
        newly_missing = archive.mark_upstream_missing(seen_ids)
        if newly_missing:
            print(
                f"  {len(newly_missing)} archived note(s) no longer upstream "
                "— flagged, not deleted."
            )

    archive.save_index()
    if high_water and counts.failed == 0:
        archive.save_state(updated_after=high_water)

    return counts
