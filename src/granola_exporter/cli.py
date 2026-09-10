"""Command-line interface for the Granola archive.

Commands:
    doctor  Validate configuration and backend reachability.
    login   Authorize the Granola MCP in a browser.
    logout  Remove the stored MCP credentials.
    sync    Back up new and changed meetings into the archive.
    verify  Check archive integrity and reconcile against a backend.

Two backends are supported. The public API is preferred wherever a key exists;
the MCP is a fallback for accounts that cannot create one, never an upgrade
path away from the higher-fidelity source.

``--profile NAME`` scopes an invocation to one Granola account: its own
credential file and its own archive subdirectory. Without it, paths are
exactly what they have always been, so nothing existing has to move.
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
    profile: str = ""


def api_key_var(profile: str) -> str:
    """The environment variable holding a profile's public API key.

    A profile scopes the MCP credential and the archive, so it must scope the
    API key too -- otherwise ``--profile work`` silently authenticates as
    whoever ``GRANOLA_API_KEY`` belongs to and writes their meetings into the
    work archive. There is deliberately **no fallback** to ``GRANOLA_API_KEY``
    for a named profile: inheriting the default key is the bug.

    Args:
        profile: A normalized profile name, or ``""`` for the default.

    Returns:
        The variable name to read.
    """
    if not profile:
        return "GRANOLA_API_KEY"
    # Profile names admit only [a-z0-9._-], so this mapping is total and
    # cannot collide: "." and "-" are the only characters needing translation.
    suffix = profile.upper().replace(".", "_").replace("-", "_")
    return f"GRANOLA_API_KEY_{suffix}"


def _config(requested: str | None = None, profile: str | None = None) -> Config:
    """Load configuration from the environment and ``.env``.

    Precedence is the real environment, then ``.env``, then the defaults --
    ``python-dotenv`` does not override an already-set variable.

    A named profile also namespaces the archive. Two accounts sharing one
    archive is not merely untidy: ``mark_upstream_missing`` sweeps by *source*
    rather than by account, so each full sync would flag the other account's
    meetings as gone from upstream, and ``.sync-state.json`` holds a single
    watermark per source, so the second account would silently skip everything
    older than the first account's high-water mark.

    Args:
        requested: An explicit ``--source``, overriding ``GRANOLA_SYNC_SOURCE``.
        profile: An explicit ``--profile``, overriding ``GRANOLA_MCP_PROFILE``.

    Returns:
        The resolved configuration.

    Raises:
        SystemExit: If the profile name is not well-formed.
    """
    from .mcp_auth import is_valid_profile, normalize_profile

    load_dotenv()
    # An explicit --profile is validated even when it normalizes to nothing,
    # so `--profile ""` reports an error rather than silently meaning "none".
    name = normalize_profile(
        profile if profile is not None else os.environ.get("GRANOLA_MCP_PROFILE", "")
    )
    if (name or profile is not None) and not is_valid_profile(name):
        raise SystemExit(
            "--profile expects 1-32 characters of letters, digits, '.', '-' or "
            f"'_', starting with a letter or digit, got {name!r}"
        )

    archive_dir = Path(os.environ.get("GRANOLA_ARCHIVE_DIR", DEFAULT_ARCHIVE).strip())
    return Config(
        api_key=os.environ.get(api_key_var(name), "").strip(),
        archive_dir=(archive_dir / name if name else archive_dir)
        .expanduser()
        .resolve(),
        source=(
            requested or os.environ.get("GRANOLA_SYNC_SOURCE", SOURCE_AUTO).strip()
        ).lower()
        or SOURCE_AUTO,
        mcp_url=os.environ.get("GRANOLA_MCP_URL", DEFAULT_MCP_URL).strip()
        or DEFAULT_MCP_URL,
        profile=name,
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


class AccountMismatch(RuntimeError):
    """Raised when a sync would write a different account into an archive."""


def _check_account(
    archive: Archive,
    current: str,
    config: Config,
    *,
    allow_change: bool,
) -> str:
    """Compare the syncing account against the one the archive already holds.

    Two people's meetings in one archive cannot be untangled afterwards: the
    ``upstream_missing`` sweep scopes by backend rather than by account, so
    each full sync would flag the other account's notes as gone from upstream,
    and there is no per-note owner in the index to sort them out by. Refusing
    up front is the only cheap moment.

    Claims the archive as a side effect when the check passes, so the next run
    takes the recorded fast path instead of re-inferring.

    Args:
        archive: The archive about to be written.
        current: The email of the account being synced, ``""`` if unknown.
        config: The loaded configuration.
        allow_change: Whether to re-claim the archive instead of refusing.

    Returns:
        A warning line for the caller to print, or ``""``.

    Raises:
        AccountMismatch: If the accounts differ and ``allow_change`` is unset.
    """
    if not current:
        # A sync that cannot identify itself is about to fail anyway; blocking
        # here would turn a transient API hiccup into a refusal to back up.
        return "could not identify the syncing account — skipping the check"

    incumbent = archive.account_email or archive.infer_account_email()
    if not incumbent or incumbent == current:
        archive.claim_account(current, _effective_source(config))
        return ""

    if not allow_change:
        raise AccountMismatch(
            f"{archive.root} holds meetings for {incumbent}, but this run is "
            f"authorized as {current}.\n"
            f"  Mixing two accounts in one archive cannot be undone.\n"
            f"  Use --profile to give {current} its own archive, or pass "
            f"--allow-account-change to re-claim this one."
        )

    archive.claim_account(current, _effective_source(config))
    return f"archive re-claimed: {incumbent} -> {current}"


def _token_path(config: Config) -> Path:
    """Resolve the credential file for this invocation's profile.

    The single place that knows the profile-to-path rule, so the five
    subcommands cannot drift apart on it.

    Args:
        config: The loaded configuration.

    Returns:
        The token cache path.
    """
    from .mcp_auth import token_store_path

    return token_store_path(config.profile)


def _mcp_storage(config: Config):
    """Build a token store for the configured endpoint.

    Imported lazily so the public API path never pays for the MCP module.

    Args:
        config: The loaded configuration.

    Returns:
        A ``FileTokenStorage`` for the configured MCP endpoint.
    """
    from .mcp_auth import FileTokenStorage

    return FileTokenStorage(_token_path(config), config.mcp_url)


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

    Never opens a browser and never starts an authorization flow: a diagnostic
    that mutates credentials is not a diagnostic.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    from .mcp_auth import known_profiles

    config = _config(getattr(args, "source", None), getattr(args, "profile", None))
    effective = _effective_source(config)
    archive = Archive(config.archive_dir)
    index = archive.load_index()

    print("granola-exporter doctor")
    print(f"  archive dir : {config.archive_dir}")
    print(f"  profile     : {config.profile or '(none — default token file)'}")
    # Listed only when there are others, so single-account users see nothing
    # new: "what did I call the second account" is a multi-account question.
    others = [name for name in known_profiles() if name != config.profile]
    if others:
        print(f"  profiles    : {', '.join(others)}")
    key_var = api_key_var(config.profile)
    reason = "" if config.source != SOURCE_AUTO else (
        f" (auto: {key_var} set)" if config.api_key else f" (auto: no {key_var})"
    )
    print(f"  sync source : {effective}{reason}")

    failed = False
    if effective == "public-api":
        failed, live_email = _doctor_public_api(config)
    else:
        failed, live_email = _doctor_mcp(config)

    by_source: dict[str, int] = {}
    for note_id in index:
        by_source[archive.archived_source(note_id)] = (
            by_source.get(archive.archived_source(note_id), 0) + 1
        )
    breakdown = ", ".join(f"{n} {s}" for s, n in sorted(by_source.items())) or "none"
    print(f"  archived    : {len(index)} notes ({breakdown})")
    # A warning, never a failure: a diagnostic that refuses to run is not a
    # diagnostic. `sync` is where this becomes a hard stop.
    incumbent = archive.account_email or archive.infer_account_email()
    print(f"  account     : {incumbent or '(unclaimed)'}")
    if incumbent and live_email and incumbent != live_email:
        print(
            f"  WARNING     : archive holds {incumbent} but you are authorized "
            f"as {live_email} — sync will refuse without --allow-account-change"
        )
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


def _doctor_public_api(config: Config) -> tuple[bool, str]:
    """Check the public API key and reachability.

    Args:
        config: The loaded configuration.

    Returns:
        Whether something failed, and the account email the key belongs to
        (``""`` when it could not be determined).
    """
    if not config.api_key:
        print("  api key     : MISSING")
        print()
        print(
            f"Set {api_key_var(config.profile)} in .env — "
            "copy .env.example to .env first."
        )
        print(
            "Create a key at: Granola -> Settings -> Connectors -> "
            "Personal API Keys (Business or Enterprise plan)."
        )
        print("No key? The MCP backend works on every plan: granola-export login")
        return True, ""

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
            email = client.account_email()
            print(f"  api account : {email or '(no notes to identify from)'}")
    except GranolaAPIError as exc:
        print(f"  ERROR       : {exc}")
        return True, ""
    return False, email


def _doctor_mcp(config: Config) -> tuple[bool, str]:
    """Check MCP credentials and, when authorized, reachability.

    Args:
        config: The loaded configuration.

    Returns:
        Whether something failed, and the authorized account's email (``""``
        when it could not be determined).
    """
    from .mcp_api import EXPECTED_TOOLS, MCPClient, MCPError
    from .mcp_auth import MCPAuthError

    storage = _mcp_storage(config)
    status = storage.status()
    print(f"  mcp endpoint: {config.mcp_url}")
    print(f"  token file  : {status.path}")
    print(f"  mcp auth    : {status.describe()}")
    if not status.present:
        return True, ""

    email = ""
    try:
        with MCPClient(
            config.mcp_url, token_path=_token_path(config), allow_login=False
        ) as client:
            tools = client.tool_names()
            print(f"  mcp tools   : OK ({len(tools)})")
            missing = EXPECTED_TOOLS - set(tools)
            if missing:
                print(f"  WARNING     : expected tools absent: {sorted(missing)}")
            account = client.account_info()
            email = str(account.get("email") or "")
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
        return True, email
    return False, email


# -- login / logout --------------------------------------------------------


def cmd_login(args: argparse.Namespace) -> int:
    """Run the interactive MCP authorization flow.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.
    """
    from .mcp_api import MCPError, login
    from .mcp_auth import MCPAuthError

    config = _config(profile=getattr(args, "profile", None))
    # Deliberately no TTY check. This flow never reads stdin -- it opens a
    # browser and waits on a loopback socket -- so a piped or captured stdin
    # says nothing about whether authorization can succeed. Whether a browser
    # actually opens is discovered by trying, and the URL is printed either way.
    try:
        account = login(
            config.mcp_url,
            token_path=_token_path(config),
            open_browser=not args.no_browser,
        )
    except (MCPAuthError, MCPError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    suffix = f" (profile: {config.profile})" if config.profile else ""
    print(f"Authorized as {account.get('email', '?')}{suffix}")
    workspace = account.get("active_workspace") or {}
    if workspace:
        print(f"Workspace: {workspace.get('display_name', '?')}")
    scopes = (account.get("mcp_note_access") or {}).get("scopes")
    if scopes:
        print(f"Note access: {', '.join(scopes)}")
    return 0


def cmd_logout(args: argparse.Namespace) -> int:
    """Remove the stored MCP credentials.

    ``--all`` sweeps every profile, which is what makes "leave nothing behind"
    practical: with several accounts, running ``logout`` once per profile is
    exactly the step people skip.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Process exit code.

    Raises:
        SystemExit: If ``--all`` is combined with ``--profile``.
    """
    from .mcp_auth import known_profiles

    profile = getattr(args, "profile", None)
    if getattr(args, "all", False):
        if profile is not None:
            raise SystemExit("--all removes every profile; drop --profile")
        targets = [None, *known_profiles()]
    else:
        targets = [profile]

    removed = False
    for name in targets:
        storage = _mcp_storage(_config(profile=name))
        if storage.clear():
            print(f"Removed {storage.path}")
            removed = True
    if not removed:
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
    config = _config(args.source, getattr(args, "profile", None))
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

    allow_change = bool(getattr(args, "allow_account_change", False))
    try:
        if effective == "public-api":
            if not config.api_key:
                print(
                    f"No {api_key_var(config.profile)}. "
                    "Run 'granola-export doctor' for setup help.",
                    file=sys.stderr,
                )
                return 1
            with PublicAPIClient(config.api_key) as client:
                # Before sync_public_api, which writes on its first note.
                note = _check_account(
                    archive, client.account_email(), config, allow_change=allow_change
                )
                if note:
                    print(f"  NOTE: {note}", file=sys.stderr)
                counts = sync_public_api(archive, client, opts)
        else:
            counts = _sync_via_mcp(archive, config, opts, allow_change=allow_change)
    except AccountMismatch as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
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


def _sync_via_mcp(
    archive: Archive,
    config: Config,
    opts: SyncOptions,
    *,
    allow_change: bool = False,
):
    """Run a sync through the MCP backend.

    Args:
        archive: The destination archive.
        config: The loaded configuration.
        opts: Per-run options.
        allow_change: Whether a different account may re-claim the archive.

    Returns:
        The per-note tally.

    Raises:
        SystemExit: If the MCP is not authorized, or the sync fails. The
            message names the remedy rather than dumping a stack trace.
    """
    from .mcp_api import MCPClient, MCPError
    from .mcp_auth import MCPAuthError

    try:
        # allow_login=False on purpose: a scheduled sync that silently blocks
        # waiting for a browser is a backup that has stopped working.
        with MCPClient(
            config.mcp_url, token_path=_token_path(config), allow_login=False
        ) as client:
            # The client is connected but nothing has been listed or written.
            note = _check_account(
                archive,
                str(client.account_info().get("email") or ""),
                config,
                allow_change=allow_change,
            )
            if note:
                print(f"  NOTE: {note}", file=sys.stderr)
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
    config = _config(getattr(args, "source", None), getattr(args, "profile", None))
    archive = Archive(config.archive_dir)
    index = archive.load_index()

    print(f"Verifying {config.archive_dir}")
    print(f"  indexed notes      : {len(index)}")
    print(f"  account            : {archive.account_email or '(unclaimed)'}")

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
        print("  upstream check     : skipped (not authorized — run 'login')")
        return

    try:
        with MCPClient(
            config.mcp_url, token_path=_token_path(config), allow_login=False
        ) as client:
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

    def add_profile(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--profile",
            metavar="NAME",
            default=None,
            help="scope to one Granola account: its own credential file and "
            "its own archive subdirectory (default: GRANOLA_MCP_PROFILE)",
        )

    doctor = sub.add_parser("doctor", help="validate config and backend reachability")
    add_source(doctor)
    add_profile(doctor)
    doctor.set_defaults(func=cmd_doctor)

    login_parser = sub.add_parser("login", help="authorize the Granola MCP")
    login_parser.add_argument(
        "--no-browser",
        action="store_true",
        help="print the authorization URL instead of opening a browser",
    )
    add_profile(login_parser)
    login_parser.set_defaults(func=cmd_login)

    logout_parser = sub.add_parser("logout", help="remove stored MCP credentials")
    add_profile(logout_parser)
    logout_parser.add_argument(
        "--all",
        action="store_true",
        help="remove every profile's credentials; cannot reach a file placed "
        "elsewhere by GRANOLA_MCP_TOKEN_FILE",
    )
    logout_parser.set_defaults(func=cmd_logout)

    sync = sub.add_parser("sync", help="fetch new and changed meetings")
    add_source(sync)
    add_profile(sync)
    sync.add_argument(
        "--full",
        action="store_true",
        help="ignore the watermark and re-check every note",
    )
    sync.add_argument(
        "--allow-account-change",
        action="store_true",
        help="let a different Granola account write into this archive, "
        "re-claiming it (two accounts in one archive cannot be untangled)",
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
    add_profile(verify)
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
