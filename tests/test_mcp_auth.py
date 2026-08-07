"""Tests for MCP credential storage and the loopback OAuth redirect.

No network and no browser: the loopback server is driven with a plain HTTP
request from the test itself.
"""

from __future__ import annotations

import json
import stat
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

import pytest

from granola_exporter.mcp_auth import (
    FileTokenStorage,
    LoopbackCallbackServer,
    MCPAuthError,
    redact,
    token_store_path,
)
from granola_exporter.secure_io import DIR_MODE, FILE_MODE

SERVER = "https://mcp.granola.ai/mcp"


def _store(tmp_path, server: str = SERVER) -> FileTokenStorage:
    """Build a storage rooted in a temp directory.

    Args:
        tmp_path: pytest temp directory.
        server: The MCP endpoint the credentials belong to.

    Returns:
        The storage under test.
    """
    return FileTokenStorage(tmp_path / "state" / "mcp-oauth.json", server)


def _seed(store: FileTokenStorage, **tokens) -> None:
    """Write a credential file directly, bypassing the SDK models.

    Args:
        store: The storage to seed.
        **tokens: Token fields to record.
    """
    store._save(
        tokens={"access_token": "a" * 40, "token_type": "Bearer", **tokens},
        obtained_at=datetime.now(UTC).isoformat(),
    )


# -- location and permissions ----------------------------------------------


def test_token_path_honours_overrides(monkeypatch, tmp_path):
    """The cache lives outside the archive, and is overridable."""
    monkeypatch.delenv("GRANOLA_MCP_TOKEN_FILE", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    assert token_store_path() == tmp_path / "xdg/granola-exporter/mcp-oauth.json"

    monkeypatch.setenv("GRANOLA_MCP_TOKEN_FILE", str(tmp_path / "explicit.json"))
    assert token_store_path() == tmp_path / "explicit.json"


def test_credentials_are_owner_only(tmp_path):
    """Credentials get the same posture SECURITY.md documents for the archive."""
    store = _store(tmp_path)
    _seed(store)

    assert stat.S_IMODE(store.path.stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == DIR_MODE
    assert not list(store.path.parent.glob("*.tmp")), "no temp file may linger"


# -- isolation and robustness ----------------------------------------------


def test_credentials_for_another_server_are_ignored(tmp_path):
    """Repointing at a different endpoint must not reuse its credentials."""
    _seed(_store(tmp_path))
    other = _store(tmp_path, "https://mcp.example.test/mcp")
    assert other.status().present is False


def test_corrupt_credentials_read_as_unauthenticated(tmp_path):
    """A truncated file must not crash a sync; it means "log in again"."""
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{not json", encoding="utf-8")
    assert store.status().present is False


def test_absent_credentials_read_as_unauthenticated(tmp_path):
    """A fresh machine reports cleanly rather than raising."""
    status = _store(tmp_path).status()
    assert status.present is False
    assert "run 'granola-export login'" in status.describe()


# -- status reporting ------------------------------------------------------


def test_status_reports_expiry_and_refresh(tmp_path):
    """`doctor` can describe the token without any network call."""
    store = _store(tmp_path)
    _seed(store, expires_in=3600, refresh_token="r" * 40, scope="personal public")

    status = store.status()
    assert status.present is True
    assert status.has_refresh_token is True
    assert status.scope == "personal public"
    assert status.expired is False
    assert "expires in" in status.describe()
    assert status.expires_at is not None
    assert status.expires_at - datetime.now(UTC) < timedelta(seconds=3601)


def test_expired_token_is_reported_as_expired(tmp_path):
    """An elapsed token is named as such."""
    store = _store(tmp_path)
    store._save(
        tokens={"access_token": "a" * 40, "expires_in": 60},
        obtained_at=(datetime.now(UTC) - timedelta(hours=2)).isoformat(),
    )
    status = store.status()
    assert status.expired is True
    assert "expired" in status.describe()


def test_missing_expiry_is_not_assumed_expired(tmp_path):
    """Guessing would send users through a login they may not need."""
    store = _store(tmp_path)
    _seed(store, refresh_token="r" * 40)
    assert store.status().expired is False


# -- registration lifecycle ------------------------------------------------


def test_redirect_port_round_trips(tmp_path):
    """DCR fixes redirect_uris, so the port must survive across runs."""
    store = _store(tmp_path)
    assert store.redirect_port() is None
    store.remember_redirect_port(51703)
    assert _store(tmp_path).redirect_port() == 51703


def test_forget_client_keeps_tokens_but_drops_registration(tmp_path):
    """Re-registering on a port clash must not force a full re-login."""
    store = _store(tmp_path)
    _seed(store)
    store._save(client_info={"client_id": "abc"})
    store.remember_redirect_port(51703)

    store.forget_client()

    assert store.status().present is True
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert "client_info" not in data
    assert "redirect_port" not in data


def test_clear_removes_the_file(tmp_path):
    """`logout` must have a supported way to revoke local credentials."""
    store = _store(tmp_path)
    _seed(store)
    assert store.clear() is True
    assert not store.path.exists()
    assert store.clear() is False, "clearing twice is not an error"


# -- loopback redirect -----------------------------------------------------


def test_callback_captures_code_and_state():
    """The happy path: the browser hits /callback and we get the code."""
    with LoopbackCallbackServer() as server:
        assert server.redirect_uri.startswith("http://127.0.0.1:")

        def visit() -> None:
            urllib.request.urlopen(
                f"{server.redirect_uri}?code=the-code&state=the-state", timeout=5
            ).read()

        threading.Thread(target=visit, daemon=True).start()
        assert server.wait(timeout=5) == ("the-code", "the-state")


def test_callback_binds_loopback_only():
    """The redirect listener must not be reachable from the network."""
    with LoopbackCallbackServer() as server:
        assert server._server.server_address[0] == "127.0.0.1"


def test_callback_rejects_other_paths():
    """Only the registered redirect path is served."""
    with LoopbackCallbackServer() as server:
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(
                f"http://127.0.0.1:{server.port}/admin?code=x", timeout=5
            )
        assert exc.value.code == 404


def test_callback_does_not_reflect_query_parameters():
    """Reflecting attacker-controlled input into the page is a needless sink."""
    with LoopbackCallbackServer() as server:
        body = urllib.request.urlopen(
            f"{server.redirect_uri}?code=abc&state=%3Cscript%3Ealert(1)%3C/script%3E",
            timeout=5,
        ).read()
        assert b"<script>" not in body
        assert b"abc" not in body


def test_callback_surfaces_a_provider_error():
    """A refused authorisation is an error, not a silent hang."""
    with LoopbackCallbackServer() as server:

        def visit() -> None:
            try:
                urllib.request.urlopen(
                    f"{server.redirect_uri}?error=access_denied", timeout=5
                ).read()
            except urllib.error.HTTPError:
                pass

        threading.Thread(target=visit, daemon=True).start()
        with pytest.raises(MCPAuthError, match="access_denied"):
            server.wait(timeout=5)


def test_callback_times_out_cleanly():
    """A user who never completes the flow gets a message, not a hang."""
    with LoopbackCallbackServer() as server:
        with pytest.raises(MCPAuthError, match="timed out"):
            server.wait(timeout=0.2)


# -- redaction -------------------------------------------------------------


def test_redact_hides_token_shaped_strings():
    """Errors from the SDK can embed a token; printing one would leak it."""
    message = f"refresh failed for {'x' * 40}"
    assert "x" * 40 not in redact(message)
    assert "<redacted>" in redact(message)
    assert redact("short error") == "short error"
