"""Client for the official Granola public API.

Base URL ``https://public-api.granola.ai/v1``. This is the supported, versioned
API and the backbone of the archive. Requires a ``grn_`` key created at
Granola -> Settings -> Connectors -> Personal API Keys (Business or Enterprise plan).

Documented rate limits are a 25-request burst per 5 seconds and a sustained 5
requests/second, so requests pass through a token bucket sized to match, with
``Retry-After``-aware backoff on 429.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import httpx

from .models import is_valid_note_id
from .ratelimit import RateLimiter

# Re-exported so ``from .public_api import RateLimiter`` keeps working.
__all__ = [
    "BASE_URL",
    "GranolaAPIError",
    "NoteNotFoundError",
    "PublicAPIClient",
    "RateLimiter",
]

BASE_URL = "https://public-api.granola.ai/v1"

BURST_CAPACITY = 25
SUSTAINED_RATE = 5.0
MAX_PAGE_SIZE = 30
MAX_RETRIES = 5


class GranolaAPIError(RuntimeError):
    """Raised when the Granola API returns an unrecoverable error."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """Initialise the error.

        Args:
            message: Human-readable description of the failure.
            status_code: The HTTP status code, when the failure was a response.
        """
        super().__init__(message)
        self.status_code = status_code


class NoteNotFoundError(GranolaAPIError):
    """Raised when a note has no generated summary and transcript yet.

    The public API returns 404 for notes that are still processing, as well as
    for notes that genuinely do not exist. Both are non-fatal during a sync.
    """


class PublicAPIClient:
    """Read-only client for the Granola public API."""

    def __init__(
        self, api_key: str, base_url: str = BASE_URL, timeout: float = 30.0
    ) -> None:
        """Initialise the client.

        Args:
            api_key: A Granola API key, expected to start with ``grn_``.
            base_url: API base URL; overridable for tests.
            timeout: Per-request timeout in seconds.

        Raises:
            GranolaAPIError: If no API key was supplied.
        """
        if not api_key:
            raise GranolaAPIError(
                "No API key. Set GRANOLA_API_KEY in .env "
                "(Granola -> Settings -> Connectors -> Personal API Keys -> Create new key)."
            )
        self.base_url = base_url.rstrip("/")
        self._limiter = RateLimiter(BURST_CAPACITY, SUSTAINED_RATE)
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": "granola-exporter/0.1.0",
            },
        )

    def __enter__(self) -> PublicAPIClient:
        """Enter the context manager.

        Returns:
            This client.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the underlying HTTP connection pool."""
        self.close()

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Perform a rate-limited GET with retry on 429 and 5xx.

        Args:
            path: Path relative to the API base URL.
            params: Optional query parameters; ``None`` values are dropped.

        Returns:
            The decoded JSON response body.

        Raises:
            NoteNotFoundError: If the resource returned 404.
            GranolaAPIError: On authentication failure, exhausted retries, or
                an undecodable response.
        """
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        url = f"{self.base_url}{path}"

        for attempt in range(MAX_RETRIES):
            self._limiter.acquire()
            try:
                response = self._client.get(url, params=clean)
            except httpx.RequestError as exc:
                if attempt == MAX_RETRIES - 1:
                    raise GranolaAPIError(f"Network error calling {path}: {exc}") from exc
                time.sleep(2.0**attempt)
                continue

            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError as exc:
                    raise GranolaAPIError(
                        f"Malformed JSON from {path}: {exc}"
                    ) from exc

            if response.status_code == 401:
                raise GranolaAPIError(
                    "Granola rejected the API key (401). Check GRANOLA_API_KEY.",
                    status_code=401,
                )
            if response.status_code == 403:
                raise GranolaAPIError(
                    "Granola denied access (403). The key may lack the required "
                    "scope, or the plan may not include API access.",
                    status_code=403,
                )
            if response.status_code == 404:
                raise NoteNotFoundError(f"Not found: {path}", status_code=404)

            if response.status_code == 429 or response.status_code >= 500:
                if attempt == MAX_RETRIES - 1:
                    break
                retry_after = response.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after else 2.0**attempt
                except ValueError:
                    delay = 2.0**attempt
                time.sleep(delay)
                continue

            raise GranolaAPIError(
                f"Unexpected {response.status_code} from {path}: "
                f"{response.text[:200]}",
                status_code=response.status_code,
            )

        raise GranolaAPIError(f"Giving up on {path} after {MAX_RETRIES} attempts.")

    def list_notes_page(
        self,
        cursor: str | None = None,
        page_size: int = MAX_PAGE_SIZE,
        updated_after: datetime | str | None = None,
        created_after: datetime | str | None = None,
        created_before: datetime | str | None = None,
        folder_id: str | None = None,
    ) -> dict[str, Any]:
        """Fetch a single page of note stubs.

        Args:
            cursor: Opaque pagination cursor from a previous page.
            page_size: Notes per page, clamped to the documented 1-30 range.
            updated_after: Only notes updated after this instant.
            created_after: Only notes created after this instant.
            created_before: Only notes created before this instant.
            folder_id: Restrict to a folder and its children (``fol_*``).

        Returns:
            The decoded page: ``{"notes": [...], "hasMore": bool,
            "cursor": str | None}``.
        """
        return self._get(
            "/notes",
            {
                "cursor": cursor,
                "page_size": max(1, min(page_size, MAX_PAGE_SIZE)),
                "updated_after": _iso(updated_after),
                "created_after": _iso(created_after),
                "created_before": _iso(created_before),
                "folder_id": folder_id,
            },
        )

    def iter_notes(
        self,
        updated_after: datetime | str | None = None,
        created_after: datetime | str | None = None,
        created_before: datetime | str | None = None,
        folder_id: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate every note stub matching the filters, following cursors.

        Args:
            updated_after: Only notes updated after this instant.
            created_after: Only notes created after this instant.
            created_before: Only notes created before this instant.
            folder_id: Restrict to a folder and its children (``fol_*``).

        Yields:
            Note stub dictionaries in the order the API returns them.

        Raises:
            GranolaAPIError: If the API repeats a cursor, which would otherwise
                loop forever.
        """
        cursor: str | None = None
        seen: set[str] = set()
        while True:
            page = self.list_notes_page(
                cursor=cursor,
                updated_after=updated_after,
                created_after=created_after,
                created_before=created_before,
                folder_id=folder_id,
            )
            notes = page.get("notes")
            if not isinstance(notes, list):
                return
            yield from (n for n in notes if isinstance(n, dict))

            if not page.get("hasMore"):
                return
            cursor = page.get("cursor")
            if not cursor:
                return
            if cursor in seen:
                raise GranolaAPIError("Pagination cursor repeated; aborting.")
            seen.add(cursor)

    def get_note(self, note_id: str, include_transcript: bool = True) -> dict[str, Any]:
        """Fetch a single note, optionally with its full transcript.

        Args:
            note_id: A public API note id (``not_*``).
            include_transcript: Whether to request the transcript inline.

        Returns:
            The decoded note payload, verbatim.

        Raises:
            NoteNotFoundError: If the note is missing or still processing.
            GranolaAPIError: If ``note_id`` is not a well-formed ``not_*`` id.
                It is validated before interpolation so that a malformed value
                cannot alter the request path.
        """
        if not is_valid_note_id(note_id):
            raise GranolaAPIError(f"Refusing to request a malformed note id: {note_id!r}")
        return self._get(
            f"/notes/{note_id}",
            {"include": "transcript"} if include_transcript else None,
        )

    def list_folders(self) -> list[dict[str, Any]]:
        """Fetch all folders accessible to the key.

        Returns:
            A list of folder dictionaries; empty if the response shape is
            unrecognised.
        """
        data = self._get("/folders")
        if isinstance(data, list):
            return [f for f in data if isinstance(f, dict)]
        for key in ("folders", "data", "items"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, list):
                return [f for f in value if isinstance(f, dict)]
        return []


def _iso(value: datetime | str | None) -> str | None:
    """Render a datetime as an ISO 8601 string for a query parameter.

    Args:
        value: A datetime, an already-formatted string, or ``None``.

    Returns:
        The ISO 8601 representation, or ``None`` when there is no value.
    """
    if value is None:
        return None
    return value.isoformat() if isinstance(value, datetime) else str(value)
