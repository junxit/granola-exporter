"""Security regression tests.

Each test here corresponds to a finding from the security review. They exist to
stop those defects from silently returning:

* untrusted note ids must not build paths outside the archive root
* a tampered ``index.json`` must not redirect writes outside the archive
* archived content and its directories must not be world-readable
* a credential profile name must not build a path outside the state directory
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from granola_exporter.mcp_auth import MCPAuthError, state_dir, token_store_path
from granola_exporter.models import (
    Note,
    is_valid_archive_key,
    is_valid_folder_id,
    is_valid_mcp_note_id,
    is_valid_note_id,
    mcp_archive_key,
)
from granola_exporter.public_api import GranolaAPIError, PublicAPIClient, RateLimiter
from granola_exporter.render import render_note, render_transcript
from granola_exporter.store import (
    DIR_MODE,
    FILE_MODE,
    Archive,
    UnsafeArchivePathError,
)

VALID_ID = "not_1d3tmYTlCICgjy"


def _note(note_id: str, created: str | None = "2026-01-27T15:30:00Z") -> Note:
    """Build a note with a given id.

    Args:
        note_id: The id to place on the note.
        created: ISO creation timestamp, or ``None`` for the undated branch.

    Returns:
        The parsed note.
    """
    return Note.from_api({"id": note_id, "title": "Meeting", "created_at": created})


# -- id validation --------------------------------------------------------


def test_valid_ids_accepted():
    """Real-world ids from the live archive must keep working."""
    for good in (VALID_ID, "not_ohhU2777kpHR22", "not_AD3bT0wpFfqNfZ"):
        assert is_valid_note_id(good)
    assert is_valid_folder_id("fol_p79L5OGbdPihPP")


@pytest.mark.parametrize(
    "bad",
    [
        "../../../../../tmp/pwned",
        "not_../../../../etc",
        "/tmp/absolute",
        "not_short",
        "not_waytoolongtobevalid123",
        "NOT_1d3tmYTlCICgjy",
        "not_1d3tmYTlCICgj/",
        "",
        None,
        123,
    ],
)
def test_malformed_ids_rejected(bad):
    """Anything that is not exactly ``not_`` + 14 alphanumerics is refused."""
    assert not is_valid_note_id(bad)


# -- path traversal (finding 1) -------------------------------------------


@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5, 6, 7])
def test_traversal_ids_never_escape(tmp_path: Path, depth: int):
    """Traversal ids are rejected outright, at every depth.

    A PoC during the review escaped the archive root at depth >= 5 (>= 4 on the
    undated branch), so every depth is covered here.
    """
    archive = Archive(tmp_path)
    note = _note("../" * depth + "tmp/pwned")
    with pytest.raises(UnsafeArchivePathError):
        archive.note_dir(note)


@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5])
def test_traversal_ids_never_escape_undated(tmp_path: Path, depth: int):
    """The undated branch has one less directory level and is covered too."""
    archive = Archive(tmp_path)
    note = _note("../" * depth + "tmp/pwned", created=None)
    with pytest.raises(UnsafeArchivePathError):
        archive.note_dir(note)


def test_absolute_id_rejected(tmp_path: Path):
    """An absolute id must not be able to redirect the write."""
    archive = Archive(tmp_path)
    with pytest.raises(UnsafeArchivePathError):
        archive.note_dir(_note("/tmp/abs_pwned"))


def test_valid_id_stays_inside_root(tmp_path: Path):
    """The happy path still resolves under the archive root."""
    archive = Archive(tmp_path)
    path = archive.note_dir(_note(VALID_ID))
    assert archive.root in path.parents


def test_title_traversal_still_neutralised(tmp_path: Path):
    """slugify() must keep stripping separators out of titles."""
    archive = Archive(tmp_path)
    note = Note.from_api(
        {"id": VALID_ID, "title": "../../etc/passwd", "created_at": "2026-01-27T15:30:00Z"}
    )
    path = archive.note_dir(note)
    assert archive.root in path.parents
    assert ".." not in path.name


# -- index tampering (finding 2) ------------------------------------------


def test_absolute_path_in_index_is_refused(tmp_path: Path):
    """A tampered index must not make is_unchanged() stat outside the archive."""
    archive = Archive(tmp_path)
    archive.load_index()["x"] = {"content_hash": "h", "path": "/etc"}
    assert archive.is_unchanged("x", "h") is False


def test_tampered_index_does_not_relocate_outside(tmp_path: Path):
    """write_note() must not shutil.move() a directory in from outside root."""
    outside = tmp_path.parent / "outside_archive"
    outside.mkdir(exist_ok=True)
    (outside / "canary.txt").write_text("do not move me", encoding="utf-8")

    archive = Archive(tmp_path / "archive")
    note = _note(VALID_ID)
    archive.load_index()[note.id] = {
        "content_hash": "stale",
        "path": str(outside),
    }

    transcript_md = render_transcript(note)
    archive.write_note(note, render_note(note, bool(transcript_md)), transcript_md)

    assert (outside / "canary.txt").is_file(), "content outside the archive was moved"
    assert archive.note_dir(note).is_dir()


# -- permissions (finding 3) ----------------------------------------------


def test_written_files_and_dirs_are_owner_only(tmp_path: Path):
    """Archived content must not inherit a world-readable umask."""
    archive = Archive(tmp_path)
    note = Note.from_api(
        {
            "id": VALID_ID,
            "title": "Sensitive meeting",
            "created_at": "2026-01-27T15:30:00Z",
            "summary_text": "private",
            "transcript": [
                {"speaker": {"name": "A"}, "text": "private", "start_time": None}
            ],
        }
    )
    transcript_md = render_transcript(note)
    archive.write_note(note, render_note(note, bool(transcript_md)), transcript_md)
    archive.save_index()

    target = archive.note_dir(note)
    assert stat.S_IMODE(target.stat().st_mode) == DIR_MODE

    for name in ("raw.json", "note.md", "transcript.md"):
        f = target / name
        assert f.is_file(), name
        mode = stat.S_IMODE(f.stat().st_mode)
        assert mode == FILE_MODE, f"{name} is {oct(mode)}, expected {oct(FILE_MODE)}"
        assert not mode & stat.S_IROTH, f"{name} is world-readable"
        assert not mode & stat.S_IRGRP, f"{name} is group-readable"

    index_mode = stat.S_IMODE(archive.index_path.stat().st_mode)
    assert index_mode == FILE_MODE, "index.json leaks titles and must be owner-only"


def test_no_world_readable_temp_window(tmp_path: Path):
    """The atomic-write temp file must not be left behind world-readable."""
    archive = Archive(tmp_path)
    archive.save_state(updated_after="2026-01-27T15:30:00Z")
    assert not list(tmp_path.glob("*.tmp")), "temp file left behind"
    assert stat.S_IMODE(archive.state_path.stat().st_mode) == FILE_MODE


# -- URL path injection (finding 1, second sink) --------------------------


def test_get_note_refuses_malformed_id_before_request():
    """A malformed id must never reach the URL builder."""
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"id": "x"})

    client = PublicAPIClient("grn_test", base_url="https://api.test/v1")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client._limiter = RateLimiter(capacity=1000, rate=1e6)

    with client:
        with pytest.raises(GranolaAPIError, match="malformed note id"):
            client.get_note("../../admin")
    assert not called, "a request was issued for a malformed id"


# -- the real archive's ids remain valid ----------------------------------


def test_live_archive_ids_all_conform():
    """Guard against the validation breaking the real archive.

    Skips when the archive is absent (CI, fresh clone).
    """
    index_path = Path(__file__).resolve().parents[1] / "archive" / "index.json"
    if not index_path.is_file():
        pytest.skip("no local archive to check")
    ids = list(json.loads(index_path.read_text(encoding="utf-8")))
    bad = [i for i in ids if not is_valid_archive_key(i)]
    assert not bad, f"validation would reject real archived ids: {bad[:5]}"

    # An `mcp_` key must satisfy the stricter MCP shape, not merely the union.
    mcp_bad = [
        i for i in ids if i.startswith("mcp_") and not is_valid_mcp_note_id(i)
    ]
    assert not mcp_bad, f"malformed MCP keys in the archive: {mcp_bad[:5]}"


# -- MCP id namespace ------------------------------------------------------


def test_valid_mcp_key_accepted():
    """A canonical MCP key round-trips through the validators."""
    key = "mcp_e96c1c66-ed02-4a32-9acd-54720f8761b1"
    assert is_valid_mcp_note_id(key)
    assert is_valid_archive_key(key)
    assert not is_valid_note_id(key), "MCP keys must not pass the public API check"


@pytest.mark.parametrize(
    "value",
    [
        "mcp_../../../../etc/passwd",
        "mcp_e96c1c66-ed02-4a32-9acd-54720f8761b1/../..",
        "mcp_e96c1c66-ed02-4a32-9acd-54720f8761b1/x",
        "mcp_E96C1C66-ED02-4A32-9ACD-54720F8761B1",  # uppercase
        "mcp_e96c1c66ed024a329acd54720f8761b1",  # no hyphens
        "mcp_e96c1c66-ed02-4a32-9acd-54720f8761b",  # short group
        "/tmp/mcp_e96c1c66-ed02-4a32-9acd-54720f8761b1",
        "mcp_",
        "e96c1c66-ed02-4a32-9acd-54720f8761b1",  # bare uuid, unprefixed
        "",
        None,
        123,
    ],
)
def test_malformed_mcp_keys_rejected(value):
    """Anything not exactly ``mcp_`` + canonical UUID is refused."""
    assert not is_valid_mcp_note_id(value)
    assert not is_valid_archive_key(value)


@pytest.mark.parametrize("depth", range(1, 8))
def test_mcp_traversal_keys_never_escape(tmp_path, note_payload, depth):
    """A traversal payload wearing an ``mcp_`` prefix cannot escape the root."""
    archive = Archive(tmp_path / "archive")
    note = Note.from_api(note_payload)
    note.id = "mcp_" + "../" * depth + "escaped"
    with pytest.raises(UnsafeArchivePathError):
        archive.note_dir(note)


def test_mcp_archive_key_builds_only_from_valid_uuids():
    """The key builder refuses anything that is not a UUID."""
    assert (
        mcp_archive_key("E96C1C66-ED02-4A32-9ACD-54720F8761B1")
        == "mcp_e96c1c66-ed02-4a32-9acd-54720f8761b1"
    ), "ids are normalized to lowercase so one meeting yields one key"
    for bad in ["../../etc", "", None, "not_1d3tmYTlCICgjy", "abc", 42]:
        assert mcp_archive_key(bad) is None


def test_get_note_refuses_an_mcp_key_before_request():
    """The security invariant: widening the path check must not widen the URL check.

    ``store.note_dir`` now accepts ``mcp_*`` keys, but the public API client
    must still reject them without issuing a request.
    """
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"id": "x"})

    client = PublicAPIClient("grn_test", base_url="https://api.test/v1")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    client._limiter = RateLimiter(capacity=1000, rate=1e6)

    with client:
        with pytest.raises(GranolaAPIError, match="malformed note id"):
            client.get_note("mcp_e96c1c66-ed02-4a32-9acd-54720f8761b1")
    assert not called, "an MCP key reached the public API URL builder"


def test_note_dir_accepts_both_namespaces_inside_root(tmp_path, note_payload):
    """Both id shapes build paths, and both stay under the archive root."""
    archive = Archive(tmp_path / "archive")
    for note_id in (
        "not_1d3tmYTlCICgjy",
        "mcp_e96c1c66-ed02-4a32-9acd-54720f8761b1",
    ):
        note = Note.from_api(note_payload)
        note.id = note_id
        target = archive.note_dir(note)
        assert archive.root in target.parents
        assert note_id in target.name


# -- credential profile names ----------------------------------------------
#
# A profile name is interpolated into the token cache filename, so it is the
# same finding class as the note-id traversal tests above.


@pytest.mark.parametrize("depth", [1, 2, 3, 4, 5, 6, 7])
def test_traversing_profile_names_never_escape(monkeypatch, tmp_path, depth: int):
    """A traversal-shaped profile is refused before any path is built."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    with pytest.raises(MCPAuthError):
        token_store_path("../" * depth + "tmp/pwned")


@pytest.mark.parametrize(
    "name", ["/etc/shadow", "a/b", "a\\b", "..", ".", "~root", "x" * 33, "na\x00me"]
)
def test_malformed_profile_names_never_build_a_path(monkeypatch, tmp_path, name: str):
    """Absolute, separator-bearing and oversized names are all refused."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    with pytest.raises(MCPAuthError):
        token_store_path(name)


@pytest.mark.parametrize("name", ["work", "personal-2", "a.b_c", "9", "x" * 32])
def test_accepted_profile_names_stay_in_the_state_directory(
    monkeypatch, tmp_path, name: str
):
    """Containment is proven by the allowlist; this pins it.

    The regex admits only a single path component, so no runtime containment
    check is needed -- but the guarantee is asserted rather than assumed.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    path = token_store_path(name)
    assert path.parent == state_dir()
    assert path.name == f"mcp-oauth-{name}.json"
