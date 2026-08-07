"""Tests for the MCP client facade.

No network: these cover the SDK-shape adapters and the request guards, driving
real ``mcp.types`` objects so an SDK bump shows up here rather than in
production.
"""

from __future__ import annotations

from datetime import date

import mcp.types as t
import pytest

from granola_exporter.mcp_api import (
    EXPECTED_TOOLS,
    MAX_MEETINGS_PER_CALL,
    MCPError,
    MCPToolError,
    MCPClient,
    _result_json,
    _result_text,
)
from granola_exporter.mcp_auth import MCPAuthError

UUID_A = "d290f1ee-6c54-4b01-90e6-d701748f0851"


def _text_result(text: str, *, is_error: bool = False) -> t.CallToolResult:
    """Build a CallToolResult carrying one text block.

    Args:
        text: The block's text.
        is_error: Whether the tool reported failure.

    Returns:
        The result object.
    """
    return t.CallToolResult(
        content=[t.TextContent(type="text", text=text)], is_error=is_error
    )


class _Recorder(MCPClient):
    """A client whose transport is replaced by a call log."""

    def __init__(self, result: t.CallToolResult | None = None) -> None:
        """Initialise without connecting.

        Args:
            result: The result every ``_call`` returns.
        """
        self.calls: list[tuple[str, dict]] = []
        self._result = result or _text_result("ok")

    def _call(self, tool, arguments=None):  # type: ignore[override]
        """Record the call instead of issuing it.

        Args:
            tool: Tool name.
            arguments: Tool arguments.

        Returns:
            The canned result.
        """
        self.calls.append((tool, arguments or {}))
        return self._result


# -- result adapters -------------------------------------------------------


def test_result_text_concatenates_blocks():
    """Text content is joined in order."""
    result = t.CallToolResult(
        content=[
            t.TextContent(type="text", text="one"),
            t.TextContent(type="text", text="two"),
        ],
        is_error=False,
    )
    assert _result_text(result, tool="x") == "one\ntwo"


def test_result_text_raises_on_tool_error_and_redacts():
    """An errored tool raises, and any token-shaped text is blanked."""
    secret = "z" * 44
    with pytest.raises(MCPToolError) as exc:
        _result_text(_text_result(f"denied {secret}", is_error=True), tool="x")
    assert secret not in str(exc.value)


def test_result_text_raises_on_empty_content():
    """Empty output is an error, not an empty archive."""
    with pytest.raises(MCPToolError, match="no text content"):
        _result_text(_text_result("   "), tool="x")


def test_result_json_prefers_structured_content():
    """Structured content wins over re-parsing the text block."""
    result = t.CallToolResult(
        content=[t.TextContent(type="text", text='{"from": "text"}')],
        is_error=False,
        structured_content={"from": "structured"},
    )
    assert _result_json(result, tool="x") == {"from": "structured"}


def test_result_json_falls_back_to_the_text_block():
    """Tools that return plain JSON text still decode."""
    assert _result_json(_text_result('{"a": 1}'), tool="x") == {"a": 1}


def test_result_json_raises_on_undecodable_text():
    """Prose where JSON was expected is drift, and must be loud."""
    with pytest.raises(MCPToolError, match="undecodable JSON"):
        _result_json(_text_result("not json at all"), tool="x")


# -- request guards --------------------------------------------------------


def test_get_meetings_rejects_an_oversized_batch():
    """The server caps batches at ten; exceeding it is a client-side error."""
    client = _Recorder()
    with pytest.raises(MCPError, match="at most 10"):
        client.get_meetings([UUID_A] * (MAX_MEETINGS_PER_CALL + 1))
    assert client.calls == [], "no request may be issued"


def test_get_meetings_rejects_an_empty_batch():
    """An empty batch is a caller bug, not a no-op request."""
    with pytest.raises(MCPError, match="at least one id"):
        _Recorder().get_meetings([])


@pytest.mark.parametrize(
    "bad",
    [
        "../../etc/passwd",
        "not-a-uuid",
        "",
        "mcp_" + UUID_A,  # an archive key is not a wire id
        UUID_A + "/..",
        UUID_A[:-1],
    ],
)
def test_malformed_ids_never_reach_a_request(bad):
    """Meeting ids become archive keys, so they are validated before use."""
    client = _Recorder()
    with pytest.raises(MCPError, match="malformed meeting id"):
        client.get_meetings([bad])
    with pytest.raises(MCPError, match="malformed meeting id"):
        client.get_meeting_transcript(bad)
    assert client.calls == [], "a malformed id reached the transport"


def test_uppercase_uuids_are_accepted_on_the_wire():
    """UUIDs are case-insensitive; rejecting one would drop a real meeting.

    Case is normalised where it matters -- when the id becomes an archive key
    -- not here, so a server that changes casing cannot cost us a note.
    """
    from granola_exporter.models import mcp_archive_key

    client = _Recorder()
    client.get_meetings([UUID_A.upper()])
    assert client.calls, "an uppercase UUID is a valid id"
    assert mcp_archive_key(UUID_A.upper()) == mcp_archive_key(UUID_A), (
        "both casings must fold to one archive key, or a meeting is archived twice"
    )


def test_list_meetings_requires_dates_for_a_custom_range():
    """A custom range without bounds would silently list the wrong window."""
    with pytest.raises(MCPError, match="needs both start and end"):
        _Recorder().list_meetings(time_range="custom")


def test_list_meetings_sends_iso_dates():
    """Dates are sent in the documented ISO form."""
    client = _Recorder()
    client.list_meetings(custom_start=date(2026, 1, 1), custom_end=date(2026, 1, 31))
    tool, args = client.calls[0]
    assert tool == "list_meetings"
    assert args["custom_start"] == "2026-01-01"
    assert args["custom_end"] == "2026-01-31"
    assert args["time_range"] == "custom"


def test_list_meetings_passes_a_folder_filter():
    """Folder membership needs one listing per folder."""
    client = _Recorder()
    client.list_meetings(time_range="last_30_days", folder_id="fid")
    _, args = client.calls[0]
    assert args["folder_id"] == "fid"
    assert "custom_start" not in args


def test_list_folders_tolerates_either_response_shape():
    """The payload has been seen as an object; a bare list is also accepted."""
    wrapped = _Recorder(
        t.CallToolResult(
            content=[], is_error=False, structured_content={"folders": [{"id": "a"}]}
        )
    )
    assert wrapped.list_folders() == [{"id": "a"}]
    listed = _Recorder(_text_result('[{"id": "b"}]'))
    assert listed.list_folders() == [{"id": "b"}]
    junk = _Recorder(_text_result('"nope"'))
    assert junk.list_folders() == []


# -- connection policy -----------------------------------------------------


def test_sync_refuses_to_start_an_interactive_login(tmp_path):
    """A scheduled sync that silently waits on a browser is a broken backup."""
    client = MCPClient(token_path=tmp_path / "absent.json", allow_login=False)
    with pytest.raises(MCPAuthError, match="granola-export login"):
        client.connect()


def test_calling_before_connecting_is_an_error(tmp_path):
    """Using an unconnected client fails loudly rather than hanging."""
    client = MCPClient(token_path=tmp_path / "absent.json")
    with pytest.raises(MCPError, match="not connected"):
        client.account_info()


def test_expected_tool_set_matches_the_documented_surface():
    """Drift in the advertised tools is worth noticing."""
    assert EXPECTED_TOOLS == {
        "get_account_info",
        "get_meeting_transcript",
        "get_meetings",
        "list_meeting_folders",
        "list_meetings",
        "query_granola_meetings",
    }
