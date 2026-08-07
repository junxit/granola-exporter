"""Synchronous client for the Granola MCP server.

The MCP SDK is async-only; the rest of this tool is synchronous. Rather than
colour the whole codebase async for one backend, the async boundary stops here:
a single worker thread owns one event loop, one transport and one session for
the client's lifetime, and tool calls are marshalled onto it.

That shape is deliberate. ``streamable_http_client`` opens an ``anyio`` task
group internally, and an anyio task group must be entered and exited from the
*same* task. Driving it with repeated ``asyncio.run`` calls, or holding it open
across them with an ``AsyncExitStack``, orphans that task group and fails at
teardown. One thread, one loop, one long-lived task is correct by construction.

Everything the SDK touches is confined to this module and the two ``_result_*``
adapters, so replacing the SDK is a one-file change.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import threading
import webbrowser
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from .mcp_auth import (
    DEFAULT_CALLBACK_TIMEOUT,
    FileTokenStorage,
    LoopbackCallbackServer,
    MCPAuthError,
    port_is_available,
    redact,
    token_store_path,
)
from .models import is_valid_uuid
from .ratelimit import RateLimiter

MCP_URL = "https://mcp.granola.ai/mcp"

# `get_meetings` accepts at most ten ids per call.
MAX_MEETINGS_PER_CALL = 10

# Documented as averaging ~100 requests/minute across all tools.
MCP_BURST = 10
MCP_RATE = 100 / 60

EXPECTED_TOOLS = frozenset(
    {
        "get_account_info",
        "get_meeting_transcript",
        "get_meetings",
        "list_meeting_folders",
        "list_meetings",
        "query_granola_meetings",
    }
)

CLIENT_NAME = "granola-exporter"
CLIENT_URI = "https://github.com/junxit/granola-exporter"

_CONNECT_TIMEOUT = 120.0


class MCPError(RuntimeError):
    """Raised when an MCP call fails unrecoverably."""


class MCPToolError(MCPError):
    """Raised when a tool reports an error or returns nothing usable."""


class MCPProtocol(Protocol):
    """The operations the sync layer needs from an MCP backend.

    Declared as a Protocol so the sync tests can drive the whole pipeline with
    a fake, offline and without importing the SDK.
    """

    def account_info(self) -> dict[str, Any]:
        """Return the connected account's email, workspace and scopes."""
        ...

    def tool_names(self) -> list[str]:
        """Return the tool names the server advertises."""
        ...

    def list_folders(self) -> list[dict[str, Any]]:
        """Return the user's meeting folders."""
        ...

    def list_meetings(
        self,
        *,
        custom_start: date | None = None,
        custom_end: date | None = None,
        time_range: str = "custom",
        folder_id: str | None = None,
    ) -> str:
        """Return the verbatim ``list_meetings`` response."""
        ...

    def get_meetings(self, meeting_ids: Sequence[str]) -> str:
        """Return the verbatim ``get_meetings`` response."""
        ...

    def get_meeting_transcript(self, meeting_id: str) -> dict[str, Any]:
        """Return the decoded ``get_meeting_transcript`` response."""
        ...


def _result_text(result: Any, *, tool: str) -> str:
    """Concatenate the text content blocks of a tool result.

    This and :func:`_result_json` are the only places that know the SDK's
    result shape, so an SDK bump touches one function. Both the snake_case
    (current) and camelCase (older) spellings are tolerated.

    Args:
        result: Whatever ``call_tool`` returned.
        tool: The tool that produced it, for error messages.

    Returns:
        The concatenated text.

    Raises:
        MCPToolError: If the tool reported an error or returned no text.
    """
    is_error = getattr(result, "is_error", None)
    if is_error is None:
        is_error = getattr(result, "isError", False)

    blocks = getattr(result, "content", None) or []
    text = "\n".join(
        block.text for block in blocks if getattr(block, "text", None) is not None
    )

    if is_error:
        raise MCPToolError(f"{tool} reported an error: {redact(text)[:400]}")
    if not text.strip():
        raise MCPToolError(f"{tool} returned no text content")
    return text


def _result_json(result: Any, *, tool: str) -> Any:
    """Decode a tool result that carries JSON.

    Args:
        result: Whatever ``call_tool`` returned.
        tool: The tool that produced it.

    Returns:
        The decoded payload, preferring structured content when present.

    Raises:
        MCPToolError: If the tool errored or the text is not valid JSON.
    """
    structured = getattr(result, "structured_content", None)
    if structured is None:
        structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict) and structured:
        return structured

    text = _result_text(result, tool=tool)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise MCPToolError(f"{tool} returned undecodable JSON: {exc}") from exc


def _client_metadata(redirect_uri: str) -> Any:
    """Build the Dynamic Client Registration metadata.

    Args:
        redirect_uri: The loopback URI the browser will be sent back to.

    Returns:
        An ``OAuthClientMetadata`` instance.
    """
    from mcp.shared.auth import OAuthClientMetadata

    return OAuthClientMetadata(
        client_name=CLIENT_NAME,
        client_uri=CLIENT_URI,
        redirect_uris=[redirect_uri],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )


class MCPClient:
    """Read-only synchronous client for the Granola MCP server."""

    def __init__(
        self,
        server_url: str = MCP_URL,
        *,
        token_path: Path | None = None,
        timeout: float = 60.0,
        allow_login: bool = False,
        open_browser: bool = True,
    ) -> None:
        """Initialise the client. No connection is made until entered.

        Args:
            server_url: The MCP endpoint.
            token_path: Where credentials live; defaults to the XDG location.
            timeout: Per-call timeout in seconds.
            allow_login: Whether an interactive browser flow may be started.
                ``sync`` and ``doctor`` pass ``False``: a scheduled sync that
                silently blocks on a browser is a broken backup.
            open_browser: Whether to launch a browser during login, as opposed
                to printing the URL for the user to open themselves.
        """
        self.server_url = server_url
        self.timeout = timeout
        self.allow_login = allow_login
        self.open_browser = open_browser
        self.storage = FileTokenStorage(token_path or token_store_path(), server_url)
        self._limiter = RateLimiter(MCP_BURST, MCP_RATE)

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session: Any = None
        self._callback: LoopbackCallbackServer | None = None
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._stop: asyncio.Event | None = None

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> MCPClient:
        """Open the session.

        Returns:
            This client.
        """
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the session."""
        self.close()

    def connect(self) -> None:
        """Start the worker thread and wait for the session to initialise.

        Raises:
            MCPAuthError: If credentials are absent or expired and interactive
                login is not permitted.
            MCPError: If the session could not be established.
        """
        if self._thread is not None:
            return

        if not self.allow_login and not self.storage.status().present:
            raise MCPAuthError(
                "not authorised for the Granola MCP — run 'granola-export login'"
            )

        if self.allow_login:
            self._callback = self._start_callback_server()

        self._thread = threading.Thread(
            target=self._run, name="granola-mcp", daemon=True
        )
        self._thread.start()

        if not self._ready.wait(_CONNECT_TIMEOUT):
            self.close()
            raise MCPError(f"timed out connecting to {self.server_url}")
        if self._startup_error is not None:
            error = self._startup_error
            self.close()
            if isinstance(error, MCPAuthError):
                raise error
            raise MCPError(f"could not connect to {self.server_url}: {redact(str(error))}")

    def close(self) -> None:
        """Tear the session down and release the worker thread."""
        if self._loop is not None and self._stop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None
        if self._callback is not None:
            self._callback.__exit__(None, None, None)
            self._callback = None
        self._loop = None
        self._session = None

    def _start_callback_server(self) -> LoopbackCallbackServer:
        """Bind the loopback redirect listener.

        Dynamic Client Registration fixes ``redirect_uris`` at registration
        time, so a previously registered port is reused when it is still free.
        When it is not, the registration is dropped and redone rather than
        failing with a redirect-URI mismatch.

        Returns:
            The running callback server.
        """
        remembered = self.storage.redirect_port()
        if remembered and port_is_available(remembered):
            server = LoopbackCallbackServer(remembered)
        else:
            if remembered:
                self.storage.forget_client()
            server = LoopbackCallbackServer(0)
        server.__enter__()
        self.storage.remember_redirect_port(server.port)
        return server

    # -- the worker thread -------------------------------------------------

    def _run(self) -> None:
        """Thread entry point: own one event loop for the session's lifetime."""
        try:
            asyncio.run(self._session_main())
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            self._startup_error = exc
        finally:
            self._ready.set()

    async def _session_main(self) -> None:
        """Hold the transport and session open until asked to stop.

        The task group inside ``streamable_http_client`` is entered and exited
        by this one task; tool calls are scheduled onto the same loop as
        separate coroutines.
        """
        import httpx2
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        redirect_uri = (
            self._callback.redirect_uri
            if self._callback
            else "http://127.0.0.1:0/callback"
        )
        provider = OAuthClientProvider(
            server_url=self.server_url,
            client_metadata=_client_metadata(redirect_uri),
            storage=self.storage,
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
        )

        async with httpx2.AsyncClient(auth=provider, timeout=self.timeout) as http:
            async with streamable_http_client(
                self.server_url, http_client=http
            ) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()

    async def _redirect_handler(self, url: str) -> None:
        """Send the user to the authorization URL.

        Args:
            url: The provider's authorization URL.

        Raises:
            MCPAuthError: When interactive login is not permitted.
        """
        if not self.allow_login:
            raise MCPAuthError(
                "the Granola MCP needs re-authorisation — "
                "run 'granola-export login'"
            )
        # flush=True throughout: this runs on the worker thread and stdout may
        # be a pipe, so without it the URL can sit in a buffer while the user
        # stares at a silent terminal waiting for something to click.
        print("\nAuthorise granola-exporter in your browser:", flush=True)
        print(f"  {url}\n", flush=True)

        opened = webbrowser.open(url) if self.open_browser else False
        if not opened:
            port = self._callback.port if self._callback else 0
            print(
                "Could not open a browser automatically — open the URL above.",
                flush=True,
            )
            print(
                "If this machine has no browser, forward the callback port "
                f"first:\n  ssh -L {port}:127.0.0.1:{port} <host>\n",
                flush=True,
            )
        print(
            f"Waiting up to {DEFAULT_CALLBACK_TIMEOUT:.0f}s for the redirect…",
            flush=True,
        )

    async def _callback_handler(self) -> Any:
        """Wait for the browser to hit the loopback redirect.

        Returns:
            The authorization code and state.

        Raises:
            MCPAuthError: If no callback server is running.
        """
        from mcp.shared.auth import AuthorizationCodeResult

        if self._callback is None:
            raise MCPAuthError("no callback server is listening")
        server = self._callback
        code, state = await asyncio.to_thread(server.wait, DEFAULT_CALLBACK_TIMEOUT)
        return AuthorizationCodeResult(code=code, state=state or None)

    # -- calling tools -----------------------------------------------------

    def _call(self, tool: str, arguments: dict[str, Any] | None = None) -> Any:
        """Invoke a tool on the session and block for its result.

        Args:
            tool: The tool name.
            arguments: The tool arguments.

        Returns:
            The raw ``CallToolResult``.

        Raises:
            MCPError: If the client is not connected, or the call fails.
        """
        if self._session is None or self._loop is None:
            raise MCPError("MCP client is not connected")
        self._limiter.acquire()
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(tool, arguments or {}), self._loop
        )
        try:
            return future.result(timeout=self.timeout + 30.0)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise MCPError(f"{tool} timed out after {self.timeout:.0f}s") from exc
        except MCPError:
            raise
        except Exception as exc:
            raise MCPError(f"{tool} failed: {redact(str(exc))}") from exc

    # -- the six operations ------------------------------------------------

    def account_info(self) -> dict[str, Any]:
        """Fetch the connected account's identity and access scopes.

        Returns:
            The decoded ``get_account_info`` payload.
        """
        return _result_json(self._call("get_account_info"), tool="get_account_info")

    def tool_names(self) -> list[str]:
        """List the tools the server advertises.

        A cheap drift check on the whole backend: a change here means the
        surface this tool is built against has moved.

        Returns:
            The advertised tool names, sorted.
        """
        if self._session is None or self._loop is None:
            raise MCPError("MCP client is not connected")
        future = asyncio.run_coroutine_threadsafe(
            self._session.list_tools(), self._loop
        )
        result = future.result(timeout=self.timeout)
        return sorted(tool.name for tool in getattr(result, "tools", []))

    def list_folders(self) -> list[dict[str, Any]]:
        """Fetch the user's meeting folders.

        Returns:
            The folder list; empty when the shape is unrecognised.
        """
        payload = _result_json(
            self._call("list_meeting_folders"), tool="list_meeting_folders"
        )
        if isinstance(payload, dict):
            folders = payload.get("folders")
            if isinstance(folders, list):
                return [f for f in folders if isinstance(f, dict)]
        if isinstance(payload, list):
            return [f for f in payload if isinstance(f, dict)]
        return []

    def list_meetings(
        self,
        *,
        custom_start: date | None = None,
        custom_end: date | None = None,
        time_range: str = "custom",
        folder_id: str | None = None,
    ) -> str:
        """List meetings in a window, returning the response verbatim.

        Args:
            custom_start: Inclusive start of a custom range.
            custom_end: Inclusive end of a custom range.
            time_range: The server's range enum; ``custom`` uses the dates.
            folder_id: Restrict to one folder.

        Returns:
            The verbatim tool output, for the parser and for ``raw.json``.
        """
        args: dict[str, Any] = {"time_range": time_range}
        if time_range == "custom":
            if custom_start is None or custom_end is None:
                raise MCPError("a custom time_range needs both start and end dates")
            args["custom_start"] = custom_start.isoformat()
            args["custom_end"] = custom_end.isoformat()
        if folder_id:
            args["folder_id"] = folder_id
        return _result_text(self._call("list_meetings", args), tool="list_meetings")

    def get_meetings(self, meeting_ids: Sequence[str]) -> str:
        """Fetch details for up to ten meetings.

        Args:
            meeting_ids: Meeting UUIDs, at most
                :data:`MAX_MEETINGS_PER_CALL`.

        Returns:
            The verbatim tool output.

        Raises:
            MCPError: If the batch is too large or any id is not a UUID. Ids
                become archive keys, so they are validated before use rather
                than trusted, as ``public_api.get_note`` does for ``not_*``.
        """
        ids = list(meeting_ids)
        if not ids:
            raise MCPError("get_meetings needs at least one id")
        if len(ids) > MAX_MEETINGS_PER_CALL:
            raise MCPError(
                f"get_meetings accepts at most {MAX_MEETINGS_PER_CALL} ids, got {len(ids)}"
            )
        for candidate in ids:
            if not is_valid_uuid(candidate):
                raise MCPError(f"refusing a malformed meeting id: {candidate!r}")
        return _result_text(
            self._call("get_meetings", {"meeting_ids": ids}), tool="get_meetings"
        )

    def get_meeting_transcript(self, meeting_id: str) -> dict[str, Any]:
        """Fetch one meeting's transcript.

        Args:
            meeting_id: A meeting UUID.

        Returns:
            The decoded payload.

        Raises:
            MCPError: If the id is not a UUID.
        """
        if not is_valid_uuid(meeting_id):
            raise MCPError(f"refusing a malformed meeting id: {meeting_id!r}")
        return _result_json(
            self._call("get_meeting_transcript", {"meeting_id": meeting_id}),
            tool="get_meeting_transcript",
        )


def login(
    server_url: str = MCP_URL,
    *,
    token_path: Path | None = None,
    open_browser: bool = True,
) -> dict[str, Any]:
    """Run the interactive OAuth flow and verify the result.

    Args:
        server_url: The MCP endpoint.
        token_path: Where to store credentials.
        open_browser: Whether to launch a browser automatically.

    Returns:
        The account info fetched with the new credentials.
    """
    with MCPClient(
        server_url,
        token_path=token_path,
        allow_login=True,
        open_browser=open_browser,
    ) as client:
        return client.account_info()
