from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from recording_config import RecordingConfig, build_recording_config
from recording_store import (
    build_audio_s3_key,
    build_s3_key,
    build_s3_url,
    build_transcript_s3_key,
)
from recording_transcript import normalize_session_report
from identity import (
    _extract_phone,
    _normalize_user_id,
    resolve_user_id_from_call_context,
    resolve_user_id_from_room_metadata,
)


# ---------------------------------------------------------------------------
# recording_config
# ---------------------------------------------------------------------------


def test_build_recording_config_defaults() -> None:
    config = build_recording_config({})
    assert config.database_url == ""
    assert config.s3_bucket == ""
    assert config.enabled is False


def test_build_recording_config_enabled() -> None:
    config = build_recording_config(
        {
            "DATABASE_URL": "postgres://localhost/test",
            "AWS_S3_BUCKET": "my-bucket",
            "AWS_DEFAULT_REGION": "ap-south-1",
            "AWS_S3_FORCE_PATH_STYLE": "1",
        }
    )
    assert config.enabled is True
    assert config.s3_region == "ap-south-1"
    assert config.s3_force_path_style is True


# ---------------------------------------------------------------------------
# recording_store: S3 key/url generation
# ---------------------------------------------------------------------------


def test_build_s3_key_deterministic() -> None:
    now = datetime(2026, 3, 15, 10, 30, 0, tzinfo=timezone.utc)
    key = build_s3_key("interview-agent", "room-abc", "audio.mp3", "agents", now)
    assert key == "agents/interview-agent/sessions/2026/03/15/room-abc/audio.mp3"


def test_build_transcript_s3_key() -> None:
    now = datetime(2026, 12, 25, 18, 0, 0, tzinfo=timezone.utc)
    key = build_transcript_s3_key("interview-agent", "room-123", "agents", now)
    assert (
        key
        == "agents/interview-agent/sessions/2026/12/25/room-123/transcript.json"
    )


def test_build_s3_url_custom_endpoint() -> None:
    url = build_s3_url(
        "my-bucket",
        "agents/room/audio.mp3",
        endpoint="https://s3.custom.io",
    )
    assert url == "https://s3.custom.io/my-bucket/agents/room/audio.mp3"


# ---------------------------------------------------------------------------
# recording_transcript: normalization
# ---------------------------------------------------------------------------


def _sample_report_dict(
    *,
    messages: list | None = None,  # pass [] for empty, None for defaults
    started_at: float | None = None,
    duration: float | None = None,
) -> dict:
    return {
        "job_id": "job-001",
        "room_id": "room-id-001",
        "room": "test-room",
        "started_at": started_at or 1711900000.0,
        "timestamp": (started_at or 1711900000.0) + (duration or 120.0),
        "duration": duration or 120.0,
        "chat_history": {
            "messages": [
                {"role": "assistant", "content": "Welcome to interview coaching."},
                {
                    "role": "user",
                    "content": "I want to prepare for a PM interview",
                    "create_time": 1711900010.0,
                },
                {
                    "role": "assistant",
                    "content": "Let's start with a behavioral question.",
                    "interrupted": True,
                },
            ]
            if messages is None
            else messages
        },
        "events": [{"type": "agent_speaking", "timestamp": 1711900001.0}],
        "options": {"llm": {"model": "openai/gpt-5.1"}},
    }


def test_normalize_basic_report() -> None:
    result = normalize_session_report(
        _sample_report_dict(),
        agent_type="interview-agent",
        agent_name="interview-coaching-agent",
        egress_id="eg-001",
        resolved_user_id="user_+919999999999",
    )
    assert result["schema_version"] == "1.0"
    assert result["session"]["agent_type"] == "interview-agent"
    assert len(result["turns"]) == 3
    assert result["turns"][2].get("interrupted") is True
    assert result["usage"]["model"] == "openai/gpt-5.1"


def test_normalize_empty_messages() -> None:
    result = normalize_session_report(
        _sample_report_dict(messages=[]),
        agent_type="interview-agent",
        agent_name="test",
    )
    assert result["turns"] == []


def test_normalize_chat_history_items() -> None:
    report = {
        "chat_history": {
            "items": [
                {
                    "id": "item_1",
                    "type": "message",
                    "role": "assistant",
                    "content": ["Welcome back"],
                    "created_at": 1711900001.0,
                    "interrupted": False,
                },
                {
                    "id": "item_2",
                    "type": "message",
                    "role": "user",
                    "content": ["I want to prepare for sales interviews"],
                    "created_at": 1711900005.0,
                    "interrupted": False,
                },
            ]
        }
    }

    result = normalize_session_report(
        report,
        agent_type="interview-agent",
        agent_name="test",
    )

    assert len(result["turns"]) == 2
    assert result["turns"][0]["text"] == "Welcome back"
    assert result["turns"][1]["role"] == "user"


def test_normalize_missing_optional_fields() -> None:
    result = normalize_session_report(
        {"chat_history": {"messages": [{"role": "user", "content": "hi"}]}},
        agent_type="interview-agent",
        agent_name="test",
    )
    assert result["session"]["started_at"] is None
    assert result["session"]["duration_seconds"] is None
    assert len(result["turns"]) == 1


# ---------------------------------------------------------------------------
# identity resolution
# ---------------------------------------------------------------------------


def test_extract_phone_sip_prefix() -> None:
    assert _extract_phone("sip_+919876543210") == "+919876543210"


def test_extract_phone_user_prefix() -> None:
    assert _extract_phone("user_+919876543210") == "+919876543210"


def test_extract_phone_no_match() -> None:
    assert _extract_phone("random-string") is None


def test_normalize_user_id_from_sip() -> None:
    assert _normalize_user_id("sip_+919876543210") == "user_+919876543210"


def test_resolve_room_metadata() -> None:
    uid = resolve_user_id_from_room_metadata('{"user_id":"user_+919999999999"}')
    assert uid == "user_+919999999999"


def test_resolve_room_metadata_invalid_json() -> None:
    uid = resolve_user_id_from_room_metadata("not-json")
    assert uid.startswith("demo_")


def test_resolve_call_context_prefers_non_demo() -> None:
    result = resolve_user_id_from_call_context(
        current_user_id="user_+911234567890",
        participant_identity="sip_+919999999999",
        participant_attributes=None,
        room_name=None,
    )
    assert result == "user_+911234567890"


def test_resolve_call_context_prefers_explicit_user_id_attribute() -> None:
    result = resolve_user_id_from_call_context(
        current_user_id="demo_abc1234",
        participant_identity="sip_+919999999999",
        participant_attributes={"user_id": "candidate_42"},
        room_name=None,
    )
    assert result == "candidate_42"


def test_resolve_call_context_falls_to_participant() -> None:
    result = resolve_user_id_from_call_context(
        current_user_id="demo_abc1234",
        participant_identity="sip_+919999999999",
        participant_attributes=None,
        room_name=None,
    )
    assert result == "user_+919999999999"


# ---------------------------------------------------------------------------
# recording_db: mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_session_calls_pool() -> None:
    import recording_db

    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"id": "sess-001"})
    recording_db._pool = mock_pool

    try:
        result = await recording_db.insert_session(
            agent_type="interview-agent",
            agent_name="interview-coaching-agent",
            livekit_room_name="test-room",
        )
        assert result == "sess-001"
    finally:
        recording_db._pool = None


@pytest.mark.asyncio
async def test_update_session_completed() -> None:
    import recording_db

    mock_pool = AsyncMock()
    mock_pool.execute = AsyncMock()
    recording_db._pool = mock_pool

    try:
        await recording_db.update_session_completed(
            "sess-001",
            ended_at=datetime.now(timezone.utc),
            status="COMPLETED",
        )
        mock_pool.execute.assert_called_once()
    finally:
        recording_db._pool = None


# ---------------------------------------------------------------------------
# recording_runtime: orchestration (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_recording_inserts_row_even_on_egress_failure() -> None:
    import recording_db
    import recording_runtime

    mock_pool = AsyncMock()
    mock_pool.fetchrow = AsyncMock(return_value={"id": "sess-002"})
    recording_db._pool = mock_pool

    mock_lk_api = MagicMock()
    mock_lk_api.egress = AsyncMock()
    mock_lk_api.egress.start_room_composite_egress = AsyncMock(
        side_effect=Exception("egress unavailable")
    )

    config = RecordingConfig(
        database_url="postgres://localhost/test",
        s3_bucket="test-bucket",
        s3_access_key="key",
        s3_secret_key="secret",
    )

    try:
        session_id, egress_id = await recording_runtime.start_recording(
            config=config,
            lk_api=mock_lk_api,
            agent_type="interview-agent",
            agent_name="interview-coaching-agent",
            room_name="test-room",
            metadata={"session_mode": "practice", "source": "callback_request"},
        )
        assert session_id == "sess-002"
        assert egress_id is None
        inserted_metadata = json.loads(mock_pool.fetchrow.await_args.args[-1])
        assert inserted_metadata["session_mode"] == "practice"
        assert inserted_metadata["source"] == "callback_request"
    finally:
        recording_db._pool = None


@pytest.mark.asyncio
async def test_finalize_recording_uploads_transcript_and_stops_egress() -> None:
    import recording_db
    import recording_runtime
    import recording_store

    mock_pool = AsyncMock()
    mock_pool.execute = AsyncMock()
    recording_db._pool = mock_pool

    mock_egress_info = MagicMock()
    mock_egress_info.status = 3  # EGRESS_COMPLETE
    mock_egress_info.error = ""

    mock_list_resp = MagicMock()
    mock_list_resp.items = [mock_egress_info]

    mock_lk_api = MagicMock()
    mock_lk_api.egress = AsyncMock()
    mock_lk_api.egress.stop_egress = AsyncMock()
    mock_lk_api.egress.list_egress = AsyncMock(return_value=mock_list_resp)

    config = RecordingConfig(
        database_url="postgres://localhost/test",
        s3_bucket="test-bucket",
        s3_access_key="key",
        s3_secret_key="secret",
    )

    with patch.object(recording_store, "upload_transcript_json", return_value="https://url"):
        try:
            await recording_runtime.finalize_recording(
                config=config,
                lk_api=mock_lk_api,
                session_id="sess-001",
                egress_id="eg-001",
                agent_type="interview-agent",
                agent_name="interview-coaching-agent",
                room_name="test-room",
                report_dict=_sample_report_dict(),
            )

            mock_lk_api.egress.stop_egress.assert_called_once()
            assert mock_pool.execute.call_count == 2
        finally:
            recording_db._pool = None
