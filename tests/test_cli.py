"""Tests for CLI configuration and backend selection.

``cli.py`` had no coverage before the MCP work; source selection in particular
decides which backend touches the archive, so it is worth pinning.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from granola_exporter.cli import (
    DEFAULT_MCP_URL,
    Config,
    _config,
    _effective_source,
    _parse_since,
    main,
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path):
    """Keep the developer's real .env and credentials out of these tests.

    Args:
        monkeypatch: pytest fixture.
        tmp_path: pytest temp directory.
    """
    monkeypatch.setattr("granola_exporter.cli.load_dotenv", lambda *a, **k: None)
    for name in (
        "GRANOLA_API_KEY",
        "GRANOLA_ARCHIVE_DIR",
        "GRANOLA_SYNC_SOURCE",
        "GRANOLA_MCP_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRANOLA_ARCHIVE_DIR", str(tmp_path / "archive"))
    monkeypatch.setenv("GRANOLA_MCP_TOKEN_FILE", str(tmp_path / "token.json"))


def _cfg(api_key: str = "", source: str = "auto") -> Config:
    """Build a config without touching the environment.

    Args:
        api_key: The public API key.
        source: The requested source.

    Returns:
        The config under test.
    """
    return Config(
        api_key=api_key,
        archive_dir=Path("/tmp/archive"),
        source=source,
        mcp_url=DEFAULT_MCP_URL,
    )


# -- source selection ------------------------------------------------------


def test_auto_prefers_the_public_api_when_a_key_exists():
    """The MCP is a fallback, never an upgrade path away from higher fidelity."""
    assert _effective_source(_cfg(api_key="grn_x")) == "public-api"


def test_auto_falls_back_to_mcp_without_a_key():
    """This is the whole point: API keys need a paid plan, the MCP does not."""
    assert _effective_source(_cfg()) == "mcp"


@pytest.mark.parametrize("requested", ["public-api", "mcp"])
def test_explicit_source_overrides_auto(requested):
    """An explicit choice is honoured even when a key is present."""
    assert _effective_source(_cfg(api_key="grn_x", source=requested)) == requested


def test_source_can_be_set_by_environment(monkeypatch):
    """`GRANOLA_SYNC_SOURCE` configures unattended runs."""
    monkeypatch.setenv("GRANOLA_SYNC_SOURCE", "mcp")
    monkeypatch.setenv("GRANOLA_API_KEY", "grn_x")
    assert _effective_source(_config()) == "mcp"


def test_cli_flag_beats_the_environment(monkeypatch):
    """An explicit --source wins over GRANOLA_SYNC_SOURCE."""
    monkeypatch.setenv("GRANOLA_SYNC_SOURCE", "mcp")
    assert _config("public-api").source == "public-api"


def test_defaults_when_nothing_is_configured():
    """A bare checkout resolves to sane defaults."""
    config = _config()
    assert config.source == "auto"
    assert config.mcp_url == DEFAULT_MCP_URL


# -- argument parsing ------------------------------------------------------


def test_parse_since_accepts_an_iso_date():
    """--since bounds an MCP backfill."""
    assert _parse_since("2026-01-01") == date(2026, 1, 1)
    assert _parse_since(None) is None


def test_parse_since_rejects_junk():
    """A misparsed date would silently scan the wrong window."""
    with pytest.raises(SystemExit, match="ISO date"):
        _parse_since("last tuesday")


def test_unknown_source_is_rejected_by_argparse(capsys):
    """Only the three known backends are accepted."""
    with pytest.raises(SystemExit):
        main(["sync", "--source", "carrier-pigeon"])
    assert "invalid choice" in capsys.readouterr().err


# -- guardrails ------------------------------------------------------------


def test_sync_without_a_key_on_the_public_api_path_fails_cleanly(capsys):
    """An explicit --source public-api with no key names the remedy."""
    assert main(["sync", "--source", "public-api"]) == 1
    assert "GRANOLA_API_KEY" in capsys.readouterr().err


def test_sync_via_mcp_without_credentials_never_opens_a_browser(capsys):
    """A scheduled sync must fail fast rather than block on a browser."""
    with pytest.raises(SystemExit) as exc:
        main(["sync", "--source", "mcp"])
    assert "granola-export login" in str(exc.value)


def test_doctor_reports_unauthorised_mcp_without_network(capsys):
    """`doctor` is a diagnostic: it neither prompts nor mutates credentials."""
    assert main(["doctor", "--source", "mcp"]) == 1
    out = capsys.readouterr().out
    assert "NOT AUTHORISED" in out
    assert "sync source : mcp" in out


def test_login_does_not_gate_on_a_tty(monkeypatch):
    """Regression: login must not refuse just because stdin is not a terminal.

    The flow never reads stdin -- it opens a browser and waits on a loopback
    socket -- so a piped or captured stdin says nothing about whether
    authorisation can succeed. Gating on it broke login under any wrapper that
    runs commands non-interactively.
    """
    called: list[bool] = []

    def fake_login(url, *, open_browser=True):
        called.append(open_browser)
        return {"email": "oat@granola.ai"}

    monkeypatch.setattr("granola_exporter.mcp_api.login", fake_login)
    monkeypatch.setattr("sys.stdin", type("NoTTY", (), {"isatty": lambda self: False})())

    assert main(["login"]) == 0
    assert called == [True], "login must still attempt the browser"


def test_logout_is_idempotent(capsys):
    """Clearing credentials that are already absent is not an error."""
    assert main(["logout"]) == 0
    assert "No stored MCP credentials" in capsys.readouterr().out


def test_verify_runs_on_an_empty_archive(capsys):
    """Verify must work before anything has been synced."""
    assert main(["verify"]) == 0
    assert "indexed notes      : 0" in capsys.readouterr().out


# -- verify: MCP reconcile -------------------------------------------------


class _FakeMCPClient:
    """Stands in for MCPClient inside cmd_verify."""

    folders: list = []
    listing: str = ""

    def __init__(self, *a, **k) -> None:
        """Accept whatever the CLI passes."""

    def __enter__(self):
        """Enter the context manager."""
        return self

    def __exit__(self, *exc):
        """Exit the context manager."""

    def list_folders(self):
        """Return the canned folder list."""
        return type(self).folders

    def list_meetings(self, **kwargs):
        """Return the canned listing for any window."""
        return type(self).listing


def _authorise(tmp_path, monkeypatch):
    """Seed a credential file so verify does not skip the MCP check.

    Args:
        tmp_path: pytest temp directory.
        monkeypatch: pytest fixture.
    """
    from granola_exporter.mcp_auth import FileTokenStorage

    store = FileTokenStorage(tmp_path / "token.json", "https://mcp.granola.ai/mcp")
    store._save(tokens={"access_token": "a" * 40}, obtained_at="2026-08-06T00:00:00+00:00")


def test_verify_deep_defaults_to_off(capsys, tmp_path, monkeypatch):
    """The cheap check is the default; --deep must be opted into."""
    import granola_exporter.mcp_api as api

    _authorise(tmp_path, monkeypatch)
    _FakeMCPClient.folders = [{"title": "Projects", "note_count": 3}]
    monkeypatch.setattr(api, "MCPClient", _FakeMCPClient)

    assert main(["verify", "--source", "mcp"]) == 0
    out = capsys.readouterr().out
    assert "Projects" in out
    assert "pass --deep" in out
    assert "upstream meetings" not in out, "the default check must not scan windows"


def test_verify_flags_a_folder_count_mismatch(capsys, tmp_path, monkeypatch):
    """A folder Granola counts but we cannot retrieve is worth surfacing."""
    import granola_exporter.mcp_api as api

    _authorise(tmp_path, monkeypatch)
    _FakeMCPClient.folders = [
        {"title": "Career", "note_count": 56},
        {"title": "Church", "note_count": 0},
    ]
    monkeypatch.setattr(api, "MCPClient", _FakeMCPClient)

    main(["verify", "--source", "mcp"])
    out = capsys.readouterr().out
    assert "Career" in out and "differs" in out
    assert "Church" in out


def test_verify_without_mcp_credentials_skips_cleanly(capsys):
    """Regression: this raised AttributeError on an unregistered --deep flag."""
    assert main(["verify", "--source", "mcp"]) == 0
    assert "not authorised" in capsys.readouterr().out


def test_verify_deep_reports_no_gap_on_an_empty_archive(capsys, tmp_path, monkeypatch):
    """An MCP that reports nothing leaves nothing un-archived."""
    import granola_exporter.mcp_api as api

    _authorise(tmp_path, monkeypatch)
    _FakeMCPClient.folders = []
    _FakeMCPClient.listing = (
        "preamble\n\n"
        '<meetings_data from="x" to="y" count="0"></meetings_data>'
    )
    monkeypatch.setattr(api, "MCPClient", _FakeMCPClient)

    assert main(["verify", "--source", "mcp", "--deep"]) == 0
    out = capsys.readouterr().out
    assert "upstream meetings  : 0" in out
    assert "gap                : none" in out
