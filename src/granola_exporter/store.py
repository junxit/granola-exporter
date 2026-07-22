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
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import Note

INDEX_NAME = "index.json"
STATE_NAME = ".sync-state.json"
RAW_NAME = "raw.json"
NOTE_NAME = "note.md"
TRANSCRIPT_NAME = "transcript.md"

MAX_SLUG_LEN = 60
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


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
        """
        self.root = root
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
        """
        created = note.created_at
        stem = f"{slugify(note.display_title)}--{note.id}" if note.id else slugify(
            note.display_title
        )
        if created is None:
            return self.root / "undated" / stem
        return (
            self.root
            / f"{created.year:04d}"
            / f"{created.month:02d}"
            / f"{created:%Y-%m-%d}--{stem}"
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
        return bool(path) and (self.root / str(path)).is_dir()

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
            old = self.root / str(previous["path"])
            if old.is_dir() and old.resolve() != target.resolve():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old), str(target))

        target.mkdir(parents=True, exist_ok=True)

        _write_json(target / RAW_NAME, note.raw)
        (target / NOTE_NAME).write_text(note_md, encoding="utf-8")

        transcript_file = target / TRANSCRIPT_NAME
        if transcript_md:
            transcript_file.write_text(transcript_md, encoding="utf-8")
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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)
