"""Owner-only filesystem primitives shared by the archive and the token cache.

The archive holds meeting transcripts and the MCP token cache holds OAuth
credentials, so both are written 0600 in 0700 directories rather than
inheriting the process umask (typically world-readable 0644/0755).

File modes are a documented guarantee in ``SECURITY.md``, so they live in one
module with one set of tests rather than being reimplemented per caller.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The archive can contain sensitive meeting content and the token cache holds
# credentials, so neither is allowed to inherit a permissive umask.
FILE_MODE = 0o600
DIR_MODE = 0o700


def secure_mkdir(path: Path) -> None:
    """Create a directory tree, owner-accessible only.

    ``Path.mkdir(mode=...)`` is subject to the umask, and with
    ``parents=True`` it creates every missing ancestor with the default mode,
    ignoring ``mode`` altogether. So the tree is built one level at a time and
    each level this call creates is chmod'ed explicitly. Ancestors that
    already existed are left alone -- one may be a home directory -- while
    ``path`` itself is always tightened, which is how a pre-existing archive
    root becomes owner-only.

    Args:
        path: Directory to create.
    """
    path = Path(path)
    created: list[Path] = []
    level = path
    while not level.is_dir() and level != level.parent:
        created.append(level)
        level = level.parent
    for level in reversed(created):
        level.mkdir(mode=DIR_MODE, exist_ok=True)
    for level in dict.fromkeys([*created, path]):
        try:
            os.chmod(level, DIR_MODE)
        except OSError:
            pass


def secure_write_text(path: Path, text: str) -> None:
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


def read_json(path: Path, default: Any) -> Any:
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


def write_json(path: Path, payload: Any) -> None:
    """Write JSON atomically, so an interrupted run cannot truncate the file.

    Args:
        path: Destination file.
        payload: JSON-serializable value.
    """
    secure_mkdir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Permissions are set on the temp file *before* the rename, so the final
    # path is never briefly world-readable.
    secure_write_text(
        tmp, json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    )
    tmp.replace(path)
