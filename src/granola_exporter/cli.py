"""Command-line interface for the Granola archive.

Commands:
    doctor  Validate configuration and backend reachability.
    login   Authorise the Granola MCP in a browser.
    logout  Remove the stored MCP credentials.
    sync    Back up new and changed meetings into the archive.
    verify  Check archive integrity and reconcile against a backend.

Two backends are supported. The public API is preferred wherever a key exists;
the MCP is a fallback for accounts that cannot create one, never an upgrade
path away from the higher-fidelity source.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .models import SOURCE_MCP, SOURCE_PUBLIC_API
from .public_api import GranolaAPIError, PublicAPIClient
from .store import Archive
from .sync import SyncOptions, scan_mcp_meeting_ids, sync_mcp, sync_public_api

DEFAULT_ARCHIVE = "./archive"
DEFAULT_MCP_URL = "https://mcp.granola.ai/mcp"

SOURCE_AUTO = "auto"
SOURCE_CHOICES = (SOURCE_AUTO, "public-api", "mcp")


@dataclass(slots=True)
class Config:
    """Resolved configuration for one invocation."""

    api_key: str
    archive_dir: Path
    source: str
    mcp_url: str


def _config(requested: str | None = None) -> Config:
    """Load configuration from the environment and ``.env``.

    Precedence is the real environment, then ``.env``, then the defaults --
    ``python-dotenv`` does not override an already-set variable.

    Args:
        requested: An explicit ``--source``, overriding ``GRANOLA_SYNC_SOURCE``.

    Returns:
        The resolved configuration.
    """
    load_dotenv()
    return Config(
        api_key=os.environ.get("GRANOLA_API_KEY", "").strip(),
        archive_dir=Path(
            os.environ.get("GRANOLA_ARCHIVE_DIR", DEFAULT_ARCHIVE).strip()
        )
        .expanduser()
        .resolve(),
        source=(
            requested or os.environ.get("GRANOLA_SYNC_SOURCE", SOURCE_AUTO).strip()
        ).lower()
        or SOURCE_AUTO,
        mcp_url=os.environ.get("GRANOLA_MCP_URL", DEFAULT_MCP_URL).strip()
        or DEFAULT_MCP_URL,
    )


def _effective_source(config: Config) -> str:
    """Resolve ``auto`` to a concrete backend.

    Args:
        config: The loaded configuration.

    Returns:
        Either ``public-api`` or ``mcp``.
    """
    if config.source in {"public-api", "mcp"}:
        return config.source
    return "public-api" if config.api_key else "mcp"


def _mcp_storage(config: Config):
    """Build a token store for the configured endpoint.

    Imported lazily so the public API path never pays for the MCP module.

    Args:
        config: The loaded configuration.

    Returns:
        A ``FileTokenStorage`` for the configured MCP endpoint.
    """
    from .mcp_auth import FileTokenStorage, token_store_path

    return FileTokenStorage(token_store_path(), config.mcp_url)


def _parse_since(value: str | None) -> date | None:
    """Parse a ``--since`` argument.

    Args:
        value: An ISO date, or ``None``.

    Returns:
        The parsed date, or ``None``.

    Raises:
        SystemExit: If the value is not an ISO date.
    """
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise SystemExit(f"--since expects an ISO date (YYYY-MM-DD), got {value!r}")


# -- doctor ----------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report configuration, credentials and backend reachability.

    Never opens a browser and never starts an authorisation flow: a diagnostic
    that mutates credentials is not a diagnostic.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    config = _config(getattr(args, "source", None))
    effective = _effective_source(config)
    archive = Archive(config.archive_dir)
    index = archive.load_index()

    print("granola-exporter doctor")
    print(f"  archive dir : {config.archive_dir}")
    reason = "" if config.source != SOURCE_AUTO else (
        " (auto: GRANOLA_API_KEY set)"
        if config.api_key
        else " (auto: no GRANOLA_API_KEY)"
    )
    print(f"  sync source : {effective}{reason}")

    failed = False
    if effective == "public-api":
        failed = _doctor_public_api(config) or failed
    else:
        failed = _doctor_mcp(config) or failed

    by_source: dict[str, int] = {}
    for note_id in index:
        by_source[archive.archived_source(note_id)] = (
            by_source.get(archive.archived_source(note_id), 0) + 1
        )
    breakdown = ", ".join(f"{n} {s}" for s, n in sorted(by_source.items())) or "none"
    print(f"  archived    : {len(index)} notes ({breakdown})")
    print(f"  watermark   : {archive.watermark or '(none — next sync is a backfill)'}")

    mcp_state = archive.source_state(SOURCE_MCP)
    if mcp_state:
        print(
            f"  mcp scan    : through {mcp_state.get('scanned_through')} "
            f"from {mcp_state.get('earliest_scanned')}; "
            f"parser v{mcp_state.get('parser_version')}"
        )
        if mcp_state.get("truncated_windows"):
            print(
                f"  WARNING     : {mcp_state['truncated_windows']} window(s) may be "
                "incomplete — re-run with --full"
            )
    return 1 if failed else 0


def _doctor_public_api(config: Config) -> bool:
    """Check the public API key and reachability.

    Args:
        config: The loaded configuration.

    Returns:
        ``True`` if something failed.
    """
    if not config.api_key:
        print("  api key     : MISSING")
        print()
        print("Set GRANOLA_API_KEY in .env — copy .env.example to .env first.")
        print(
            "Create a key at: Granola -> Settings -> Connectors -> "
            "Personal API Keys (Business or Enterprise plan)."
        )
        print("No key? The MCP backend works on every plan: granola-export login")
        return True

    masked = (
        f"{config.api_key[:8]}...{config.api_key[-4:]}"
        if len(config.api_key) > 14
        else "set"
    )
    print(f"  api key     : {masked}")
    if not config.api_key.startswith("grn_"):
        print("  warning     : key does not start with 'grn_'")

    try:
        with PublicAPIClient(config.api_key) as client:
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
        return True
    return False


def _doctor_mcp(config: Config) -> bool:
    """Check MCP credentials and, when authorised, reachability.

    Args:
        config: The loaded configuration.

    Returns:
        ``True`` if something failed.
    """
    from .mcp_api import EXPECTED_TOOLS, MCPClient, MCPError
    from .mcp_auth import MCPAuthError

    storage = _mcp_storage(config)
    status = storage.status()
    print(f"  mcp endpoint: {config.mcp_url}")
    print(f"  token file  : {status.path}")
    print(f"  mcp auth    : {status.describe()}")
    if not status.present:
        return True

    try:
        with MCPClient(config.mcp_url, allow_login=False) as client:
            tools = client.tool_names()
            print(f"  mcp tools   : OK ({len(tools)})")
            missing = EXPECTED_TOOLS - set(tools)
            if missing:
                print(f"  WARNING     : expected tools absent: {sorted(missing)}")
            account = client.account_info()
            print(f"  mcp account : {account.get('email', '?')}")
            workspace = account.get("active_workspace") or {}
            if workspace:
                print(f"  mcp workspace: {workspace.get('display_name', '?')}")
            scopes = (account.get("mcp_note_access") or {}).get("scopes")
            if scopes:
                print(f"  mcp scopes  : {', '.join(scopes)}")
            folders = client.list_folders()
            print(f"  mcp folders : OK ({len(folders)} folders)")
    except (MCPAuthError, MCPError) as exc:
        print(f"  ERROR       : {exc}")
        return True
    return False


# -- login / logout --------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    """Run the interactive MCP authorisation flow.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    from .mcp_api import MCPError, login
    from .mcp_auth import MCPAuthError

    config = _config()
    # Deliberately no TTY check. This flow never reads stdin -- it opens a
    # browser and waits on a loopback socket -- so a piped or captured stdin
    # says nothing about whether authorisation can succeed. Whether a browser
    # actually opens is discovered by trying, and the URL is printed either way.
    try:
        account = login(config.mcp_url, open_browser=not args.no_browser)
    except (MCPAuthError, MCPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"Authorised as {account.get('email', '?')}")
    workspace = account.get("active_workspace") or {}
    if workspace:
        print(f"Workspace: {workspace.get('display_name', '?')}")
    scopes = (account.get("mcp_note_access") or {}).get("scopes")
    if scopes:
        print(f"Note access: {', '.join(scopes)}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Remove the stored MCP credentials.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    storage = _mcp_storage(_config())
    if storage.clear():
        print(f"Removed {storage.path}")
    else:
        print("No stored MCP credentials.")
    return 0


# -- sync ------------------------------------------------------------------


def cmd_sync(args: argparse.Namespace) -> int:
    """Fetch new and changed meetings into the archive.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    config = _config(args.source)
    effective = _effective_source(config)
    archive = Archive(config.archive_dir)
    opts = SyncOptions(
        full=bool(args.full),
        verbose=bool(args.verbose),
        since=_parse_since(args.since),
    )
    if args.refresh_batch is not None:
        opts.refresh_batch = args.refresh_batch
    if args.window is not None:
        opts.window_days = args.window

    try:
        if effective == "public-api":
            if not config.api_key:
                print(
                    "No GRANOLA_API_KEY. Run 'granola-export doctor' for setup help.",
                    file=sys.stderr,
                )
                return 1
            with PublicAPIClient(config.api_key) as client:
                counts = sync_public_api(archive, client, opts)
        else:
            counts = _sync_via_mcp(archive, config, opts)
    except GranolaAPIError as exc:
        # The sync functions save the index before re-raising.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted — saving progress.", file=sys.stderr)
        archive.save_index()
        return 130

    print(f"Done: {counts.summary()}")
    for warning in counts.warnings():
        print(f"  WARNING: {warning}", file=sys.stderr)
    return 1 if counts.failed else 0


def _sync_via_mcp(archive: Archive, config: Config, opts: SyncOptions):
    """Run a sync through the MCP backend.

    Args:
        archive: The destination archive.
        config: The loaded configuration.
        opts: Per-run options.

    Returns:
        The per-note tally.

    Raises:
        SystemExit: If the MCP is not authorised, or the sync fails. The
            message names the remedy rather than dumping a stack trace.
    """
    from .mcp_api import MCPClient, MCPError
    from .mcp_auth import MCPAuthError

    try:
        # allow_login=False on purpose: a scheduled sync that silently blocks
        # waiting for a browser is a backup that has stopped working.
        with MCPClient(config.mcp_url, allow_login=False) as client:
            return sync_mcp(archive, client, opts, server_url=config.mcp_url)
    except MCPAuthError as exc:
        archive.save_index()
        raise SystemExit(f"ERROR: {exc}")
    except MCPError as exc:
        archive.save_index()
        raise SystemExit(f"ERROR: {exc}")


# -- verify ----------------------------------------------------------------


def cmd_verify(args: argparse.Namespace) -> int:
    """Check archive integrity and reconcile against a backend.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    config = _config(getattr(args, "source", None))
    archive = Archive(config.archive_dir)
    index = archive.load_index()

    print(f"Verifying {config.archive_dir}")
    print(f"  indexed notes      : {len(index)}")

    missing_dirs = []
    missing_raw = []
    with_transcript = 0
    degraded = 0
    undated = 0
    for note_id, entry in index.items():
        path = archive.root / str(entry.get("path", ""))
        if not path.is_dir():
            missing_dirs.append(note_id)
            continue
        if not (path / "raw.json").is_file():
            missing_raw.append(note_id)
        if entry.get("has_transcript"):
            with_transcript += 1
        if entry.get("degraded"):
            degraded += 1
        if str(entry.get("path", "")).startswith("undated/"):
            undated += 1

    by_source: dict[str, int] = {}
    for note_id in index:
        source = archive.archived_source(note_id)
        by_source[source] = by_source.get(source, 0) + 1
    for source, count in sorted(by_source.items()):
        print(f"  from {source:<18}: {count}")

    print(f"  with transcript    : {with_transcript}")
    if degraded:
        print(f"  degraded (MCP)     : {degraded}")
    if undated:
        print(f"  undated            : {undated}")
    print(f"  missing directories: {len(missing_dirs)}")
    print(f"  missing raw.json   : {len(missing_raw)}")

    # The promotion invariant: one meeting, one directory. A UUID appearing
    # under two keys means both backends archived it separately.
    duplicates = {u: keys for u, keys in archive.uuid_index().items() if len(keys) > 1}
    if duplicates:
        print(f"  DUPLICATED         : {len(duplicates)} uuid(s) archived twice")
        for uuid, keys in list(duplicates.items())[:5]:
            print(f"    {uuid}: {', '.join(keys)}")
    else:
        print("  duplicates         : none")

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

    if _effective_source(config) == "public-api" and config.api_key:
        try:
            with PublicAPIClient(config.api_key) as client:
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
    elif _effective_source(config) == "mcp":
        _verify_against_mcp(archive, config, index, folders, deep=bool(args.deep))

    return 1 if (missing_dirs or missing_raw or duplicates) else 0


def _verify_against_mcp(
    archive: Archive,
    config: Config,
    index: dict,
    archived_folders: dict[str, int],
    *,
    deep: bool,
) -> None:
    """Reconcile the archive against the MCP.

    The cheap check costs one request: folder note counts, compared by folder
    *name*, which is the only key the two backends share. ``--deep`` pays for a
    full windowed listing scan and reports a real per-meeting gap.

    Args:
        archive: The archive being verified.
        config: The loaded configuration.
        index: The loaded index.
        archived_folders: Archived note counts per folder name.
        deep: Whether to run the full scan.
    """
    from .mcp_api import MCPClient, MCPError
    from .mcp_auth import MCPAuthError

    storage = _mcp_storage(config)
    if not storage.status().present:
        print("  upstream check     : skipped (not authorised — run 'login')")
        return

    try:
        with MCPClient(config.mcp_url, allow_login=False) as client:
            print("  folder counts (MCP vs archived, joined by name):")
            for folder in client.list_folders():
                name = str(folder.get("title") or folder.get("name") or "")
                upstream = folder.get("note_count")
                mine = archived_folders.get(name, 0)
                if not name or not isinstance(upstream, int):
                    continue
                flag = "" if upstream == mine else "   <-- differs"
                print(f"    {name:<20} MCP {upstream:>4}   archived {mine:>4}{flag}")

            if not deep:
                print("  (pass --deep for a full per-meeting gap check)")
                return

            state = archive.source_state(SOURCE_MCP)
            # Without a prior MCP sync there is no recorded floor, so fall back
            # to the oldest note already archived rather than guessing a date.
            floor = state.get("earliest_scanned") or min(
                (
                    str(entry.get("created_at") or "")[:10]
                    for entry in index.values()
                    if entry.get("created_at")
                ),
                default=date.today().isoformat(),
            )
            seen, list_calls = scan_mcp_meeting_ids(
                client, date.fromisoformat(floor), date.today()
            )

            gap = seen - set(archive.uuid_index())
            print(f"  upstream meetings  : {len(seen)} ({list_calls} listings)")
            if gap:
                print(f"  NOT ARCHIVED       : {len(gap)} — run 'sync --source mcp'")
                for uuid in sorted(gap)[:10]:
                    print(f"    {uuid}")
            else:
                print("  gap                : none — archive covers every MCP meeting")
    except (MCPAuthError, MCPError) as exc:
        print(f"  upstream check     : skipped ({exc})")


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

    def add_source(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--source",
            choices=SOURCE_CHOICES,
            default=None,
            help="backend to use (default: auto — public API when a key exists)",
        )

    doctor = sub.add_parser("doctor", help="validate config and backend reachability")
    add_source(doctor)
    doctor.set_defaults(func=cmd_doctor)

    login_parser = sub.add_parser("login", help="authorise the Granola MCP")
    login_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorisation URL instead of opening a browser",
    )
    login_parser.set_defaults(func=cmd_login)

    logout_parser = sub.add_parser("logout", help="remove stored MCP credentials")
    logout_parser.set_defaults(func=cmd_logout)

    sync = sub.add_parser("sync", help="fetch new and changed meetings")
    add_source(sync)
    sync.add_argument(
        "--full",
        action="store_true",
        help="ignore the watermark and re-check every note",
    )
    sync.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="earliest meeting date to scan (MCP backfill only)",
    )
    sync.add_argument(
        "--window",
        type=int,
        default=None,
        metavar="DAYS",
        help="trailing days an MCP sync rescans for edits (default: 31)",
    )
    sync.add_argument(
        "--refresh-batch",
        type=int,
        default=None,
        metavar="N",
        help="MCP notes to re-read per run to catch older edits (default: 30)",
    )
    sync.add_argument("-v", "--verbose", action="store_true", help="per-note output")
    sync.set_defaults(func=cmd_sync)

    verify = sub.add_parser("verify", help="check integrity and reconcile with a backend")
    add_source(verify)
    verify.add_argument(
        "--deep",
        action="store_true",
        help="scan every MCP listing window for a true per-meeting gap "
        "(costs one request per 31 days; the default check costs one)",
    )
    verify.set_defaults(func=cmd_verify)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
