"""OAuth credential storage and the loopback redirect for the MCP backend.

The Granola MCP offers no API keys or service accounts: the only way in is
browser-based OAuth 2.0 with Dynamic Client Registration. That means a CLI has
to persist a token, which this module does with the same posture the archive
uses -- 0600 in a 0700 directory -- and outside the archive, so backing the
archive up or syncing it elsewhere never carries credentials.

The MCP SDK is imported lazily inside the methods that need it, so ``doctor``
can report authentication state without paying for the SDK import or touching
the network.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlparse

from .secure_io import read_json, secure_mkdir, write_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

STORAGE_VERSION = 1
DEFAULT_CALLBACK_PATH = "/callback"
DEFAULT_CALLBACK_TIMEOUT = 300.0

_SUCCESS_BODY = (
    b"<!doctype html><meta charset=utf-8><title>granola-exporter</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Authorised</h1><p>You can close this tab and return to the terminal."
    b"</p></body>"
)
_FAILURE_BODY = (
    b"<!doctype html><meta charset=utf-8><title>granola-exporter</title>"
    b"<body style='font-family:system-ui;padding:3rem'>"
    b"<h1>Authorisation failed</h1><p>Check the terminal for details.</p></body>"
)


class MCPAuthError(RuntimeError):
    """Raised when the MCP cannot be authenticated."""


def token_store_path() -> Path:
    """Resolve where the OAuth token cache lives.

    Honours ``GRANOLA_MCP_TOKEN_FILE`` then ``XDG_STATE_HOME``, defaulting to
    ``~/.local/state/granola-exporter/mcp-oauth.json``. Deliberately outside
    the archive directory: the archive is something a user may back up or sync
    to another machine, and credentials must not travel with it.

    Returns:
        The token cache path.
    """
    override = os.environ.get("GRANOLA_MCP_TOKEN_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local/state"
    return base / "granola-exporter" / "mcp-oauth.json"


@dataclass(slots=True)
class AuthStatus:
    """What ``doctor`` can say about MCP credentials without a network call."""

    present: bool
    path: Path
    server_url: str = ""
    expires_at: datetime | None = None
    has_refresh_token: bool = False
    scope: str = ""
    redirect_port: int | None = None

    @property
    def expired(self) -> bool:
        """Whether the access token is known to have expired.

        Returns:
            ``True`` only when an expiry is recorded and is in the past. A
            missing expiry is not treated as expired -- the refresh token may
            still work, and guessing would send users through a needless login.
        """
        return self.expires_at is not None and self.expires_at <= datetime.now(UTC)

    def describe(self) -> str:
        """Render a one-line summary for ``doctor``.

        Returns:
            A human-readable description of the credential state.
        """
        if not self.present:
            return "NOT AUTHORISED — run 'granola-export login'"
        if self.expires_at is None:
            bit = "no expiry recorded"
        elif self.expired:
            bit = "access token expired"
        else:
            minutes = int((self.expires_at - datetime.now(UTC)).total_seconds() // 60)
            bit = f"access token expires in {minutes}m"
        refresh = "refresh available" if self.has_refresh_token else "no refresh token"
        return f"authorised ({bit}, {refresh})"


class FileTokenStorage:
    """Persist OAuth client registration and tokens for one MCP server.

    Satisfies ``mcp.client.auth.TokenStorage``. The file records which
    ``server_url`` it belongs to, so pointing the tool at a different endpoint
    never silently reuses another endpoint's credentials.
    """

    def __init__(self, path: Path, server_url: str) -> None:
        """Initialise the store.

        Args:
            path: Where the credentials live.
            server_url: The MCP endpoint these credentials are for.
        """
        self.path = Path(path).expanduser()
        self.server_url = server_url

    # -- raw file access ---------------------------------------------------

    def _load(self) -> dict[str, Any]:
        """Read the credential file, ignoring one that belongs elsewhere.

        Returns:
            The stored mapping, or an empty one when absent, corrupt, or
            recorded against a different server.
        """
        data = read_json(self.path, default={})
        if not isinstance(data, dict):
            return {}
        if data.get("server_url") and data.get("server_url") != self.server_url:
            return {}
        return data

    def _save(self, **updates: Any) -> None:
        """Merge updates into the credential file.

        Args:
            **updates: Keys to set.
        """
        data = self._load()
        data.update(updates)
        data["version"] = STORAGE_VERSION
        data["server_url"] = self.server_url
        secure_mkdir(self.path.parent)
        write_json(self.path, data)

    # -- TokenStorage protocol --------------------------------------------

    async def get_tokens(self) -> OAuthToken | None:
        """Read the stored OAuth tokens.

        Returns:
            The tokens, or ``None`` when absent or unreadable.
        """
        from mcp.shared.auth import OAuthToken

        raw = self._load().get("tokens")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthToken.model_validate(raw)
        except Exception:
            return None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        """Persist OAuth tokens.

        ``OAuthToken`` carries a relative ``expires_in``, so the moment of
        issue is recorded alongside it; otherwise ``doctor`` could not report
        an expiry without attempting a refresh.

        Args:
            tokens: The tokens to store.
        """
        self._save(
            tokens=tokens.model_dump(mode="json", exclude_none=True),
            obtained_at=datetime.now(UTC).isoformat(),
        )

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        """Read the dynamic client registration.

        Returns:
            The registration, or ``None`` when the client is not yet
            registered.
        """
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._load().get("client_info")
        if not isinstance(raw, dict):
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            return None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        """Persist the dynamic client registration.

        Args:
            client_info: The registration returned by the server.
        """
        self._save(client_info=client_info.model_dump(mode="json", exclude_none=True))

    # -- synchronous helpers for doctor / login ---------------------------

    def status(self) -> AuthStatus:
        """Summarise the stored credentials without any network access.

        Returns:
            The credential state.
        """
        data = self._load()
        tokens = data.get("tokens")
        if not isinstance(tokens, dict) or not tokens.get("access_token"):
            return AuthStatus(present=False, path=self.path)

        expires_at = None
        obtained = data.get("obtained_at")
        expires_in = tokens.get("expires_in")
        if obtained and isinstance(expires_in, (int, float)):
            try:
                expires_at = datetime.fromisoformat(str(obtained)) + timedelta(
                    seconds=float(expires_in)
                )
            except ValueError:
                expires_at = None

        port = data.get("redirect_port")
        return AuthStatus(
            present=True,
            path=self.path,
            server_url=str(data.get("server_url") or ""),
            expires_at=expires_at,
            has_refresh_token=bool(tokens.get("refresh_token")),
            scope=str(tokens.get("scope") or ""),
            redirect_port=int(port) if isinstance(port, int) else None,
        )

    def redirect_port(self) -> int | None:
        """The loopback port this client registered its redirect URI against.

        Returns:
            The port, or ``None`` when the client has not registered yet.
        """
        port = self._load().get("redirect_port")
        return int(port) if isinstance(port, int) else None

    def remember_redirect_port(self, port: int) -> None:
        """Record the loopback port used during registration.

        Dynamic Client Registration fixes ``redirect_uris`` at registration
        time, so the same port has to be offered on every later run.

        Args:
            port: The loopback port.
        """
        self._save(redirect_port=int(port))

    def forget_client(self) -> None:
        """Drop the registration, forcing a fresh one on the next login."""
        data = self._load()
        data.pop("client_info", None)
        data.pop("redirect_port", None)
        secure_mkdir(self.path.parent)
        write_json(self.path, data)

    def clear(self) -> bool:
        """Delete the credential file entirely.

        Returns:
            ``True`` if a file was removed.
        """
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise MCPAuthError(f"could not remove {self.path}: {exc}") from exc


class _CallbackHandler(BaseHTTPRequestHandler):
    """Serves exactly one OAuth redirect and records its parameters."""

    server: _CallbackServer

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        """Capture the authorization code, or reject an unexpected path."""
        parsed = urlparse(self.path)
        if parsed.path != self.server.callback_path:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        self.server.error = (params.get("error") or [""])[0]
        self.server.code = (params.get("code") or [""])[0]
        self.server.state = (params.get("state") or [""])[0]

        ok = bool(self.server.code) and not self.server.error
        body = _SUCCESS_BODY if ok else _FAILURE_BODY
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        # The response never echoes the query string back: reflecting
        # attacker-controlled parameters into a page served on localhost is a
        # needless XSS sink for something that only has to say "done".
        self.wfile.write(body)
        self.server.received.set()

    def log_message(self, *args: Any) -> None:
        """Silence the default stderr access log."""


class _CallbackServer(HTTPServer):
    """An ``HTTPServer`` carrying the captured redirect parameters."""

    def __init__(self, address: tuple[str, int], path: str) -> None:
        """Initialise the server.

        Args:
            address: The bind address.
            path: The redirect path to accept.
        """
        super().__init__(address, _CallbackHandler)
        self.callback_path = path
        self.code = ""
        self.state = ""
        self.error = ""
        self.received = threading.Event()


class LoopbackCallbackServer:
    """Single-shot loopback HTTP server for the OAuth redirect.

    Binds ``127.0.0.1`` explicitly -- never ``0.0.0.0`` -- so the callback is
    not reachable from the network.
    """

    def __init__(self, port: int = 0, path: str = DEFAULT_CALLBACK_PATH) -> None:
        """Initialise the server.

        Args:
            port: The port to bind; ``0`` picks an ephemeral one.
            path: The redirect path to accept.
        """
        self._server = _CallbackServer(("127.0.0.1", port), path)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The bound port.

        Returns:
            The TCP port the server is listening on.
        """
        return int(self._server.server_address[1])

    @property
    def redirect_uri(self) -> str:
        """The redirect URI to register and to send the browser to.

        Returns:
            The full loopback callback URL.
        """
        return f"http://127.0.0.1:{self.port}{self._server.callback_path}"

    def __enter__(self) -> LoopbackCallbackServer:
        """Start serving in the background.

        Returns:
            This server.
        """
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Stop serving and release the port."""
        self._server.shutdown()
        self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def wait(self, timeout: float = DEFAULT_CALLBACK_TIMEOUT) -> tuple[str, str]:
        """Block until the browser hits the callback.

        Args:
            timeout: How long to wait, in seconds.

        Returns:
            The authorization code and state.

        Raises:
            MCPAuthError: On timeout, or when the provider reported an error.
        """
        if not self._server.received.wait(timeout):
            raise MCPAuthError(
                f"timed out after {timeout:.0f}s waiting for the OAuth redirect"
            )
        if self._server.error:
            raise MCPAuthError(f"authorisation was refused: {self._server.error}")
        if not self._server.code:
            raise MCPAuthError("the OAuth redirect carried no authorization code")
        return self._server.code, self._server.state


def port_is_available(port: int) -> bool:
    """Check whether a loopback port can still be bound.

    Args:
        port: The port to test.

    Returns:
        ``True`` if the port is free.
    """
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def redact(text: str) -> str:
    """Blank out anything token-shaped before printing.

    Args:
        text: A message that may embed a credential.

    Returns:
        The message with long opaque strings replaced.
    """
    return re.sub(r"[A-Za-z0-9_\-\.]{24,}", "<redacted>", text)
