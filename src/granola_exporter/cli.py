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

from .public_api import GranolaAPIError, PublicAPIClient
from .store import Archive
from .sync import SyncOptions, sync_public_api

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
    opts = SyncOptions(full=bool(args.full), verbose=bool(args.verbose))

    try:
        with PublicAPIClient(api_key) as client:
            counts = sync_public_api(archive, client, opts)
    except GranolaAPIError as exc:
        # sync_public_api saved the index before re-raising.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted — saving progress.", file=sys.stderr)
        return 130

    print(f"Done: {counts.summary()}")
    return 1 if counts.failed else 0


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
