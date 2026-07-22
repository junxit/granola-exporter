"""Shared fixtures: payloads shaped exactly like the documented API schema."""

from __future__ import annotations

import pytest


@pytest.fixture
def note_payload() -> dict:
    """A full note payload as returned by ``GET /v1/notes/{id}?include=transcript``.

    Returns:
        A decoded note payload exercising every documented field.
    """
    return {
        "id": "not_1d3tmYTlCICgjy",
        "object": "note",
        "title": "Quarterly yoghurt budget: review",
        "owner": {"name": "Oat Benson", "email": "oat@granola.ai"},
        "created_at": "2026-01-27T15:30:00Z",
        "updated_at": "2026-01-27T16:45:00Z",
        "web_url": "https://notes.granola.ai/d/d290f1ee-6c54-4b01-90e6-d701748f0851",
        "calendar_event": {
            "event_title": "Quarterly yoghurt budget",
            "invitees": ["oat@granola.ai", "milk@granola.ai"],
            "organiser": "oat@granola.ai",
            "calendar_event_id": "evt_123",
            "scheduled_start_time": "2026-01-27T15:30:00Z",
            "scheduled_end_time": "2026-01-27T16:00:00Z",
        },
        "attendees": [
            {"name": "Oat Benson", "email": "oat@granola.ai"},
            {"name": "Milk Jones", "email": "milk@granola.ai"},
        ],
        "folder_membership": [
            {"id": "fol_aaaaaaaaaaaaaa", "name": "Projects", "parent_folder_id": None},
            {
                "id": "fol_bbbbbbbbbbbbbb",
                "name": "Clients",
                "parent_folder_id": "fol_aaaaaaaaaaaaaa",
            },
        ],
        "summary_text": "We reviewed the budget.",
        "summary_markdown": "## Decisions\n\n- Approved the budget.",
        "transcript": [
            {
                "speaker": {
                    "source": "microphone",
                    "diarization_label": "Speaker A",
                    "name": "Oat Benson",
                },
                "text": "Hello everyone.",
                "start_time": "2026-01-27T15:30:00Z",
                "end_time": "2026-01-27T15:30:02Z",
            },
            {
                "speaker": {
                    "source": "microphone",
                    "diarization_label": "Speaker A",
                    "name": "Oat Benson",
                },
                "text": "Let us begin.",
                "start_time": "2026-01-27T15:30:03Z",
                "end_time": "2026-01-27T15:30:05Z",
            },
            {
                "speaker": {
                    "source": "speaker",
                    "diarization_label": "Speaker B",
                    "name": "",
                },
                "text": "Sounds good.",
                "start_time": "2026-01-27T15:31:10Z",
                "end_time": "2026-01-27T15:31:12Z",
            },
        ],
    }


@pytest.fixture
def stub_payload(note_payload: dict) -> dict:
    """A note stub as returned by ``GET /v1/notes``.

    Args:
        note_payload: The full note fixture.

    Returns:
        The subset of fields the list endpoint returns.
    """
    return {
        key: note_payload[key]
        for key in ("id", "object", "title", "owner", "created_at", "updated_at")
    }
