from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from identity import (
    _extract_phone,
    _normalize_user_id,
    resolve_user_id_from_call_context,
    resolve_user_id_from_room_metadata,
)
from recording import (
    RecordingConfig,
    build_audio_s3_key,
    build_metrics_s3_key,
    build_recording_config,
    build_s3_key,
    build_s3_url,
    build_transcript_s3_key,
    normalize_metrics_payload,
    normalize_session_report,
)

# ---------------------------------------------------------------------------
# recording_config
# ---------------------------------------------------------------------------


def test_build_recording_config_defaults() -> None:
    config = build_recording_config({})
    assert config.s3_egress_enabled is True
    assert config.s3_bucket == ""
    assert config.webhook_url == ""
    assert config.enabled is False


def test_build_recording_config_enabled() -> None:
    config = build_recording_config(
        {
            "ENABLE_RECORDING": "true",
            "AWS_S3_BUCKET": "my-bucket",
            "AWS_DEFAULT_REGION": "ap-south-1",
            "AWS_S3_FORCE_PATH_STYLE": "1",
            "WEBHOOK_URL": "https://example.com/webhook",
        }
    )
    assert config.enabled is True
    assert config.s3_region == "ap-south-1"
    assert config.s3_force_path_style is True
    assert config.webhook_url == "https://example.com/webhook"


def test_build_recording_config_disabled_by_toggle() -> None:
    config = build_recording_config(
        {
            "ENABLE_RECORDING": "false",
            "AWS_S3_BUCKET": "my-bucket",
        }
    )
    assert config.s3_egress_enabled is False
    assert config.enabled is False


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
    assert key == "agents/interview-agent/sessions/2026/12/25/room-123/transcript.json"


def test_build_s3_url_custom_endpoint() -> None:
    url = build_s3_url(
        "my-bucket",
        "agents/room/audio.mp3",
        endpoint="https://s3.custom.io",
    )
    assert url == "https://s3.custom.io/my-bucket/agents/room/audio.mp3"


def test_build_metrics_s3_key() -> None:
    now = datetime(2026, 12, 25, 18, 0, 0, tzinfo=timezone.utc)
    key = build_metrics_s3_key("interview-agent", "room-123", "agents", now)
    assert key == "agents/interview-agent/sessions/2026/12/25/room-123/metrics.json"


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
        agent_name="agent-template",
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


def test_normalize_metrics_payload_includes_usage_summary_and_events() -> None:
    result = normalize_metrics_payload(
        _sample_report_dict(),
        agent_type="interview-agent",
        agent_name="test",
        events=[{"type": "llm", "timestamp": "2026-01-01T00:00:00+00:00"}],
        usage_summary={"total_tokens": 42},
        resolved_user_id="user_1",
    )

    assert result["usage_summary"] == {"total_tokens": 42}
    assert result["metadata"]["event_count"] == 1
    assert result["subject"]["resolved_user_id"] == "user_1"


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
# recording_runtime: orchestration (mocked)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_recording_returns_audio_location_even_on_egress_failure() -> None:
    import recording

    mock_lk_api = MagicMock()
    mock_lk_api.egress = AsyncMock()
    mock_lk_api.egress.start_room_composite_egress = AsyncMock(
        side_effect=Exception("egress unavailable")
    )

    config = RecordingConfig(
        s3_bucket="test-bucket",
        s3_access_key="key",
        s3_secret_key="secret",
    )

    audio_url, audio_s3_key, egress_id = await recording.start_recording(
        config=config,
        lk_api=mock_lk_api,
        agent_type="agent-template",
        agent_name="agent-template",
        room_name="test-room",
        metadata={"interaction_mode": "auto", "source": "callback_request"},
    )

    assert audio_url.endswith("/test-room/audio.mp3")
    assert audio_s3_key.endswith("/test-room/audio.mp3")
    assert egress_id is None


@pytest.mark.asyncio
async def test_finalize_recording_uploads_transcript_and_triggers_webhook() -> None:
    import recording

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
        s3_bucket="test-bucket",
        s3_access_key="key",
        s3_secret_key="secret",
        webhook_url="https://example.com/webhook",
    )

    with (
        patch.object(
            recording, "upload_transcript_json", return_value="https://url"
        ) as upload_mock,
        patch.object(
            recording, "upload_metrics_json", return_value="https://metrics"
        ) as metrics_mock,
        patch.object(
            recording, "_post_completion_webhook", new_callable=AsyncMock
        ) as webhook_mock,
    ):
        await recording.finalize_recording(
            config=config,
            lk_api=mock_lk_api,
            egress_id="eg-001",
            agent_type="interview-agent",
            agent_name="agent-template",
            room_name="test-room",
            audio_url="https://bucket.s3.us-east-1.amazonaws.com/agents/interview-agent/sessions/test-room/audio.mp3",
            audio_s3_key="agents/interview-agent/sessions/test-room/audio.mp3",
            report_dict=_sample_report_dict(),
        )

    mock_lk_api.egress.stop_egress.assert_called_once()
    upload_mock.assert_called_once()
    metrics_mock.assert_called_once()
    webhook_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_recording_skips_webhook_when_egress_fails() -> None:
    import recording

    mock_egress_info = MagicMock()
    mock_egress_info.status = 4  # EGRESS_FAILED
    mock_egress_info.error = "failure"

    mock_list_resp = MagicMock()
    mock_list_resp.items = [mock_egress_info]

    mock_lk_api = MagicMock()
    mock_lk_api.egress = AsyncMock()
    mock_lk_api.egress.stop_egress = AsyncMock()
    mock_lk_api.egress.list_egress = AsyncMock(return_value=mock_list_resp)

    config = RecordingConfig(
        s3_bucket="test-bucket",
        s3_access_key="key",
        s3_secret_key="secret",
        webhook_url="https://example.com/webhook",
    )

    with (
        patch.object(recording, "upload_transcript_json", return_value="https://url"),
        patch.object(recording, "upload_metrics_json", return_value="https://metrics"),
        patch.object(
            recording, "_post_completion_webhook", new_callable=AsyncMock
        ) as webhook_mock,
    ):
        await recording.finalize_recording(
            config=config,
            lk_api=mock_lk_api,
            egress_id="eg-001",
            agent_type="interview-agent",
            agent_name="agent-template",
            room_name="test-room",
            audio_url="https://bucket.s3.us-east-1.amazonaws.com/agents/interview-agent/sessions/test-room/audio.mp3",
            audio_s3_key="agents/interview-agent/sessions/test-room/audio.mp3",
            report_dict=_sample_report_dict(),
        )

    webhook_mock.assert_not_awaited()
