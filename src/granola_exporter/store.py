"""Archive layout, index and sync state.

Layout::

    archive/
      2026/01/2026-01-27--quarterly-yoghurt-budget--not_1d3tmYTlCICgjy/
        note.md
        transcript.md
        raw.json
      index.json
      .sync-state.json

``raw.json`` is written before any rendering, so changing a Markdown template
never requires refetching from the API.

The archive is append-mostly. A note that disappears upstream is *never*
deleted locally -- it is flagged ``upstream_missing`` in the index, because
surviving upstream deletion is the entire point of keeping an archive.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Note, is_valid_note_id

INDEX_NAME = "index.json"
STATE_NAME = ".sync-state.json"
RAW_NAME = "raw.json"
NOTE_NAME = "note.md"
TRANSCRIPT_NAME = "transcript.md"

MAX_SLUG_LEN = 60
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")

# The archive holds meeting transcripts and may contain sensitive personal
# information, so it is kept readable only by its owner rather than inheriting
# the process umask (typically 0644/0755, i.e. world-readable).
FILE_MODE = 0o600
DIR_MODE = 0o700


class UnsafeArchivePathError(ValueError):
    """Raised when a computed path would fall outside the archive root.

    Note ids and index entries are attacker-influenced in the sense that they
    come from the API or from a file on disk. Building paths from them without
    a containment check would allow writes outside the archive.
    """


def _resolve_within(root: Path, *parts: str) -> Path:
    """Join path components under ``root`` and verify the result stays inside.

    Guards against both ``..`` traversal and absolute components -- note that
    ``Path("/a") / "/etc"`` yields ``/etc``, silently discarding the base.

    Args:
        root: The archive root that the result must remain inside.
        *parts: Path components to join beneath ``root``.

    Returns:
        The resolved path, guaranteed to be ``root`` or a descendant of it.

    Raises:
        UnsafeArchivePathError: If the joined path escapes ``root``.
    """
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise UnsafeArchivePathError(
            f"path escapes the archive root: {'/'.join(parts)!r}"
        )
    return candidate


def _secure_mkdir(path: Path) -> None:
    """Create a directory tree, owner-accessible only.

    ``Path.mkdir(mode=...)`` is subject to the umask, so the mode is applied
    explicitly afterwards to each level created under the archive.

    Args:
        path: Directory to create.
    """
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        os.chmod(path, DIR_MODE)
    except OSError:
        pass


def _secure_write_text(path: Path, text: str) -> None:
    """Write text to ``path`` with owner-only permissions.

    Args:
        path: Destination file.
        text: Contents to write.
    """
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass


def slugify(title: str) -> str:
    """Reduce a meeting title to a filesystem-safe slug.

    Args:
        title: The raw meeting title.

    Returns:
        A lowercase hyphenated slug, truncated to a sane length, or
        ``"untitled"`` when the title yields nothing usable.
    """
    slug = _SLUG_STRIP.sub("-", title.lower()).strip("-")
    if len(slug) > MAX_SLUG_LEN:
        slug = slug[:MAX_SLUG_LEN].rstrip("-")
    return slug or "untitled"


def content_hash(payload: Any) -> str:
    """Hash an API payload to detect real changes between syncs.

    Args:
        payload: The decoded API payload.

    Returns:
        A hex SHA-256 digest over the payload's canonical JSON form.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class SyncResult:
    """Per-note outcome of a sync pass."""

    note_id: str
    path: Path
    status: str  # "new" | "updated" | "unchanged"


class Archive:
    """Reads and writes the on-disk meeting archive."""

    def __init__(self, root: Path) -> None:
        """Initialise the archive.

        Args:
            root: Directory the archive lives in; created on first write.
                The path is resolved so that containment checks and
                ``relative_to`` comparisons operate on a single canonical form
                (on macOS, for example, ``/var`` resolves to ``/private/var``).
        """
        self.root = Path(root).expanduser().resolve()
        self._index: dict[str, dict[str, Any]] | None = None
        self._state: dict[str, Any] | None = None

    # -- index -------------------------------------------------------------

    @property
    def index_path(self) -> Path:
        """Path to the archive index.

        Returns:
            The ``index.json`` path.
        """
        return self.root / INDEX_NAME

    @property
    def state_path(self) -> Path:
        """Path to the sync state file.

        Returns:
            The ``.sync-state.json`` path.
        """
        return self.root / STATE_NAME

    def load_index(self) -> dict[str, dict[str, Any]]:
        """Load the archive index, caching it for the process lifetime.

        Returns:
            A mapping of note id to its index entry; empty on first run or if
            the index is unreadable.
        """
        if self._index is None:
            self._index = _read_json(self.index_path, default={})
            if not isinstance(self._index, dict):
                self._index = {}
        return self._index

    def save_index(self) -> None:
        """Write the index to disk atomically."""
        _write_json(self.index_path, self.load_index())

    def load_state(self) -> dict[str, Any]:
        """Load sync state, caching it for the process lifetime.

        Returns:
            The sync state mapping; empty on first run.
        """
        if self._state is None:
            self._state = _read_json(self.state_path, default={})
            if not isinstance(self._state, dict):
                self._state = {}
        return self._state

    def save_state(self, **updates: Any) -> None:
        """Merge updates into the sync state and persist it.

        Args:
            **updates: Keys to set on the state mapping.
        """
        state = self.load_state()
        state.update(updates)
        _write_json(self.state_path, state)

    @property
    def watermark(self) -> str | None:
        """The ``updated_at`` high-water mark from the last successful sync.

        Returns:
            An ISO 8601 timestamp, or ``None`` if no sync has completed.
        """
        value = self.load_state().get("updated_after")
        return str(value) if value else None

    # -- paths -------------------------------------------------------------

    def note_dir(self, note: Note) -> Path:
        """Compute the archive directory for a note.

        Args:
            note: The parsed note.

        Returns:
            A ``<root>/YYYY/MM/YYYY-MM-DD--slug--<id>`` path. Notes without a
            creation timestamp are filed under ``undated/``.

        Raises:
            UnsafeArchivePathError: If the note id is not a well-formed
                ``not_*`` id, or the computed path would escape the archive.
        """
        created = note.created_at
        slug = slugify(note.display_title)
        if note.id and not is_valid_note_id(note.id):
            raise UnsafeArchivePathError(f"malformed note id: {note.id!r}")
        stem = f"{slug}--{note.id}" if note.id else slug
        if created is None:
            return _resolve_within(self.root, "undated", stem)
        return _resolve_within(
            self.root,
            f"{created.year:04d}",
            f"{created.month:02d}",
            f"{created:%Y-%m-%d}--{stem}",
        )

    # -- writing -----------------------------------------------------------

    def is_unchanged(self, note_id: str, digest: str) -> bool:
        """Check whether a note's payload is byte-identical to the archived one.

        Args:
            note_id: The public API note id.
            digest: The freshly computed content hash.

        Returns:
            ``True`` if the note is already archived with this exact payload
            and its directory still exists on disk.
        """
        entry = self.load_index().get(note_id)
        if not entry or entry.get("content_hash") != digest:
            return False
        path = entry.get("path")
        if not path:
            return False
        try:
            return _resolve_within(self.root, str(path)).is_dir()
        except UnsafeArchivePathError:
            # A tampered or corrupted index must not be trusted to point
            # outside the archive; treat it as "not archived" and re-fetch.
            return False

    def write_note(
        self, note: Note, note_md: str, transcript_md: str | None
    ) -> SyncResult:
        """Write a note's raw payload and rendered Markdown into the archive.

        The verbatim payload is written first so that a rendering failure can
        never cost data. If the note's computed path has moved -- because its
        title or date changed -- the previous directory is relocated rather
        than left behind as a duplicate.

        Args:
            note: The parsed note.
            note_md: Rendered ``note.md`` contents.
            transcript_md: Rendered ``transcript.md`` contents, or ``None``
                when the note has no transcript.

        Returns:
            The outcome for this note.
        """
        index = self.load_index()
        previous = index.get(note.id)
        target = self.note_dir(note)

        if previous and previous.get("path"):
            try:
                old = _resolve_within(self.root, str(previous["path"]))
            except UnsafeArchivePathError:
                # Never relocate based on an index entry pointing outside the
                # archive; fall through and write the note fresh at `target`.
                old = None
            if old is not None and old.is_dir() and old != target:
                _secure_mkdir(target.parent)
                shutil.move(str(old), str(target))

        _secure_mkdir(target)

        _write_json(target / RAW_NAME, note.raw)
        _secure_write_text(target / NOTE_NAME, note_md)

        transcript_file = target / TRANSCRIPT_NAME
        if transcript_md:
            _secure_write_text(transcript_file, transcript_md)
        elif transcript_file.exists():
            # A transcript we already archived must survive upstream deletion.
            pass

        index[note.id] = {
            "path": str(target.relative_to(self.root)),
            "title": note.display_title,
            "uuid": note.uuid,
            "created_at": note.created_at.isoformat() if note.created_at else None,
            "updated_at": note.updated_at.isoformat() if note.updated_at else None,
            "folders": [f.name for f in note.folder_membership],
            "has_transcript": bool(note.transcript),
            "content_hash": content_hash(note.raw),
            "archived_at": datetime.now().astimezone().isoformat(),
            "upstream_missing": False,
        }
        self._index = index
        return SyncResult(
            note_id=note.id,
            path=target,
            status="updated" if previous else "new",
        )

    def mark_upstream_missing(self, note_ids: set[str]) -> list[str]:
        """Flag archived notes that no longer appear upstream.

        Nothing is deleted. The flag records that Granola no longer serves the
        note, which is exactly the case the archive exists to protect against.

        Args:
            note_ids: Ids seen in the current full listing.

        Returns:
            The ids newly flagged as missing.
        """
        index = self.load_index()
        newly_missing = []
        for note_id, entry in index.items():
            missing = note_id not in note_ids
            if missing and not entry.get("upstream_missing"):
                entry["upstream_missing"] = True
                entry["missing_since"] = datetime.now().astimezone().isoformat()
                newly_missing.append(note_id)
            elif not missing and entry.get("upstream_missing"):
                entry["upstream_missing"] = False
                entry.pop("missing_since", None)
        self._index = index
        return newly_missing


def _read_json(path: Path, default: Any) -> Any:
    """Read a JSON file, tolerating absence and corruption.

    Args:
        path: File to read.
        default: Value to return when the file is missing or unparseable.

    Returns:
        The decoded contents, or ``default``.
    """
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically, so an interrupted run cannot truncate the file.

    Args:
        path: Destination file.
        payload: JSON-serialisable value.
    """
    _secure_mkdir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Permissions are set on the temp file *before* the rename, so the final
    # path is never briefly world-readable.
    _secure_write_text(
        tmp, json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    tmp.replace(path)
