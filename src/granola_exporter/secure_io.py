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

    ``Path.mkdir(mode=...)`` is subject to the umask, so the mode is applied
    explicitly afterwards to each level created.

    Args:
        path: Directory to create.
    """
    path.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
    try:
        os.chmod(path, DIR_MODE)
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
