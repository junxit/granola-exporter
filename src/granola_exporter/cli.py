"""Command-line interface for the Granola archive.

Commands:
    doctor  Validate configuration and API reachability.
    sync    Back up new and changed meetings into the archive.
    verify  Check archive integrity and reconcile against the API.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .models import Note
from .public_api import (
    GranolaAPIError,
    NoteNotFoundError,
    PublicAPIClient,
)
from .render import render_note, render_transcript
from .store import Archive, content_hash

DEFAULT_ARCHIVE = "./archive"


def _config() -> tuple[str, Path]:
    """Load configuration from the environment and ``.env``.

    Returns:
        The API key and the resolved archive root.
    """
    load_dotenv()
    api_key = os.environ.get("GRANOLA_API_KEY", "").strip()
    archive_dir = os.environ.get("GRANOLA_ARCHIVE_DIR", DEFAULT_ARCHIVE).strip()
    return api_key, Path(archive_dir).expanduser().resolve()


def _stub_updated_at(stub: dict[str, Any]) -> str | None:
    """Read a note stub's ``updated_at`` without parsing it.

    Args:
        stub: A note stub from the list endpoint.

    Returns:
        The raw ``updated_at`` string, or ``None`` when absent.
    """
    value = stub.get("updated_at")
    return str(value) if value else None


def cmd_doctor(args: argparse.Namespace) -> int:
    """Validate the API key, reachability and archive location.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    api_key, archive_dir = _config()

    print("granola-exporter doctor")
    print(f"  archive dir : {archive_dir}")
    if not api_key:
        print("  api key     : MISSING")
        print()
        print("Set GRANOLA_API_KEY in .env — copy .env.example to .env first.")
        print("Create a key at: Granola -> Settings -> Connectors -> Personal API Keys -> Create new key")
        return 1

    masked = f"{api_key[:8]}...{api_key[-4:]}" if len(api_key) > 14 else "set"
    print(f"  api key     : {masked}")
    if not api_key.startswith("grn_"):
        print("  warning     : key does not start with 'grn_'")

    try:
        with PublicAPIClient(api_key) as client:
            page = client.list_notes_page(page_size=1)
            notes = page.get("notes") or []
            print(f"  /v1/notes   : OK (hasMore={bool(page.get('hasMore'))})")

            try:
                folders = client.list_folders()
                print(f"  /v1/folders : OK ({len(folders)} folders)")
            except GranolaAPIError as exc:
                print(f"  /v1/folders : {exc}")

            if notes:
                stub = notes[0]
                print(f"  sample note : {stub.get('id')} — {stub.get('title')!r}")
    except GranolaAPIError as exc:
        print(f"  ERROR       : {exc}")
        return 1

    archive = Archive(archive_dir)
    index = archive.load_index()
    print(f"  archived    : {len(index)} notes")
    print(f"  watermark   : {archive.watermark or '(none — next sync is a backfill)'}")
    return 0


def cmd_sync(args: argparse.Namespace) -> int:
    """Fetch new and changed meetings into the archive.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    api_key, archive_dir = _config()
    if not api_key:
        print("No GRANOLA_API_KEY. Run 'granola-export doctor' for setup help.")
        return 1

    archive = Archive(archive_dir)
    index = archive.load_index()
    full = bool(args.full) or not archive.watermark
    updated_after = None if full else archive.watermark

    mode = "full backfill" if full else f"incremental since {updated_after}"
    print(f"Syncing ({mode}) -> {archive_dir}")

    counts = {"new": 0, "updated": 0, "unchanged": 0, "skipped": 0, "failed": 0}
    seen_ids: set[str] = set()
    high_water: str | None = archive.watermark
    detail_fetches = 0

    try:
        with PublicAPIClient(api_key) as client:
            for stub in client.iter_notes(updated_after=updated_after):
                note_id = str(stub.get("id") or "")
                if not note_id:
                    continue
                seen_ids.add(note_id)

                stub_updated = _stub_updated_at(stub)
                if stub_updated and (high_water is None or stub_updated > high_water):
                    high_water = stub_updated

                # Skip the detail call entirely when the stub proves nothing
                # has changed. This is what makes a no-op re-sync cheap.
                entry = index.get(note_id)
                if (
                    entry
                    and stub_updated
                    and entry.get("updated_at") == stub_updated
                    and (archive.root / str(entry.get("path", ""))).is_dir()
                ):
                    counts["unchanged"] += 1
                    continue

                try:
                    payload = client.get_note(note_id, include_transcript=True)
                    detail_fetches += 1
                except NoteNotFoundError:
                    # Still processing, or has no summary/transcript yet.
                    counts["skipped"] += 1
                    if args.verbose:
                        print(f"  skip  {note_id} — no summary/transcript yet")
                    continue
                except GranolaAPIError as exc:
                    counts["failed"] += 1
                    print(f"  FAIL  {note_id} — {exc}", file=sys.stderr)
                    continue

                note = Note.from_api(payload)
                if not note.id:
                    note.id = note_id

                if archive.is_unchanged(note.id, content_hash(note.raw)):
                    counts["unchanged"] += 1
                    continue

                transcript_md = render_transcript(note)
                note_md = render_note(note, has_transcript_file=bool(transcript_md))
                result = archive.write_note(note, note_md, transcript_md)
                counts[result.status] += 1
                if args.verbose:
                    print(f"  {result.status:9} {note.display_title}")
    except GranolaAPIError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        archive.save_index()
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted — saving progress.", file=sys.stderr)
        archive.save_index()
        return 130

    if full:
        newly_missing = archive.mark_upstream_missing(seen_ids)
        if newly_missing:
            print(
                f"  {len(newly_missing)} archived note(s) no longer upstream "
                "— flagged, not deleted."
            )

    archive.save_index()
    if high_water and counts["failed"] == 0:
        archive.save_state(updated_after=high_water)

    print(
        f"Done: {counts['new']} new, {counts['updated']} updated, "
        f"{counts['unchanged']} unchanged, {counts['skipped']} skipped, "
        f"{counts['failed']} failed ({detail_fetches} detail fetches)"
    )
    return 1 if counts["failed"] else 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Reconcile the archive against the API and check on-disk integrity.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    api_key, archive_dir = _config()
    archive = Archive(archive_dir)
    index = archive.load_index()

    print(f"Verifying {archive_dir}")
    print(f"  indexed notes      : {len(index)}")

    missing_dirs = []
    missing_raw = []
    with_transcript = 0
    for note_id, entry in index.items():
        path = archive.root / str(entry.get("path", ""))
        if not path.is_dir():
            missing_dirs.append(note_id)
            continue
        if not (path / "raw.json").is_file():
            missing_raw.append(note_id)
        if entry.get("has_transcript"):
            with_transcript += 1

    print(f"  with transcript    : {with_transcript}")
    print(f"  missing directories: {len(missing_dirs)}")
    print(f"  missing raw.json   : {len(missing_raw)}")

    flagged = [k for k, v in index.items() if v.get("upstream_missing")]
    if flagged:
        print(f"  upstream-missing   : {len(flagged)} (retained locally)")

    folders: dict[str, int] = {}
    for entry in index.values():
        for name in entry.get("folders") or []:
            folders[name] = folders.get(name, 0) + 1
    if folders:
        print("  folders:")
        for name, count in sorted(folders.items(), key=lambda kv: -kv[1]):
            print(f"    {name:<20} {count}")

    if api_key:
        try:
            with PublicAPIClient(api_key) as client:
                upstream = {
                    str(s.get("id")) for s in client.iter_notes() if s.get("id")
                }
            print(f"  upstream notes     : {len(upstream)}")
            gap = upstream - set(index)
            if gap:
                print(f"  NOT ARCHIVED       : {len(gap)} — run 'sync'")
                for note_id in sorted(gap)[:10]:
                    print(f"    {note_id}")
            else:
                print("  gap                : none — archive covers all upstream notes")
        except GranolaAPIError as exc:
            print(f"  upstream check     : skipped ({exc})")

    return 1 if (missing_dirs or missing_raw) else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``granola-export`` command.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="granola-export",
        description="Maintain a local archive of Granola meetings.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="validate config and API reachability")
    doctor.set_defaults(func=cmd_doctor)

    sync = sub.add_parser("sync", help="fetch new and changed meetings")
    sync.add_argument(
        "--full",
        action="store_true",
        help="ignore the watermark and re-check every note",
    )
    sync.add_argument("-v", "--verbose", action="store_true", help="per-note output")
    sync.set_defaults(func=cmd_sync)

    verify = sub.add_parser("verify", help="check integrity and reconcile with the API")
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
