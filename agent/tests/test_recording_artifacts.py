from __future__ import annotations

from datetime import datetime, timezone

from recording_db import CREATE_TABLE_SQL
from recording_store import build_verbose_s3_key, build_video_s3_key
from recording_transcript import normalize_session_report, normalize_verbose_payload


def test_build_verbose_s3_key_uses_verbose_filename() -> None:
    key = build_verbose_s3_key(
        "diagnostic-agent",
        "room-1",
        "agents",
        datetime(2026, 5, 18, tzinfo=timezone.utc),
    )

    assert key == "agents/diagnostic-agent/sessions/2026/05/18/room-1/verbose.json"


def test_build_video_s3_key_uses_video_filename() -> None:
    key = build_video_s3_key(
        "diagnostic-agent",
        "room-1",
        "agents",
        datetime(2026, 5, 18, tzinfo=timezone.utc),
    )

    assert key == "agents/diagnostic-agent/sessions/2026/05/18/room-1/video.mp4"


def test_recording_db_schema_has_verbose_columns() -> None:
    assert "verbose_url" in CREATE_TABLE_SQL
    assert "verbose_s3_key" in CREATE_TABLE_SQL


def test_recording_db_schema_has_video_columns() -> None:
    assert "video_url" in CREATE_TABLE_SQL
    assert "video_s3_key" in CREATE_TABLE_SQL
    assert "video_egress_id" in CREATE_TABLE_SQL


def test_normalize_verbose_payload_preserves_tool_calls_and_outputs() -> None:
    report_dict = {
        "job_id": "AJ_123",
        "room_id": "RM_123",
        "room": "diagnostic_room",
        "started_at": 1_768_737_016.0,
        "timestamp": 1_768_737_276.0,
        "duration": 260.0,
        "usage": [{"llm": "usage"}],
        "chat_history": {
            "items": [
                {
                    "id": "config_1",
                    "type": "agent_config_update",
                    "tools_added": ["retrieve_knowledge", "mark_question_started"],
                    "created_at": 1_768_737_019.0,
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "user",
                    "content": ["start"],
                    "created_at": 1_768_737_020.0,
                },
                {
                    "id": "call_1",
                    "type": "function_call",
                    "call_id": "fc_1",
                    "name": "retrieve_knowledge",
                    "arguments": '{"query":"systems"}',
                    "created_at": 1_768_737_021.0,
                },
                {
                    "id": "out_1",
                    "type": "function_call_output",
                    "call_id": "fc_1",
                    "name": "retrieve_knowledge",
                    "output": '{"status":"ok"}',
                    "is_error": False,
                    "created_at": 1_768_737_022.0,
                },
            ]
        },
        "events": [
            {
                "type": "function_tools_executed",
                "function_calls": [
                    {
                        "type": "function_call",
                        "call_id": "fc_1",
                        "name": "retrieve_knowledge",
                        "arguments": '{"query":"systems"}',
                    }
                ],
                "function_call_outputs": [
                    {
                        "type": "function_call_output",
                        "call_id": "fc_1",
                        "name": "retrieve_knowledge",
                        "output": '{"status":"ok"}',
                        "is_error": False,
                    }
                ],
            }
        ],
    }

    payload = normalize_verbose_payload(
        report_dict,
        agent_type="diagnostic-agent",
        agent_name="diagnostic-agent",
        egress_id="EG_123",
        egress_status="EGRESS_COMPLETE",
        resolved_user_id="user-1",
        participant_identity="participant-1",
    )

    items = payload["chat_history"]["items"]
    assert [item["type"] for item in items] == [
        "agent_config_update",
        "message",
        "function_call",
        "function_call_output",
    ]
    assert payload["events"][0]["type"] == "function_tools_executed"
    assert payload["tools"]["configured"] == [
        "retrieve_knowledge",
        "mark_question_started",
    ]
    assert payload["tools"]["calls"] == [
        {
            "call_id": "fc_1",
            "name": "retrieve_knowledge",
            "arguments": '{"query":"systems"}',
            "created_at": "2026-01-18T11:50:21+00:00",
            "output": '{"status":"ok"}',
            "is_error": False,
            "output_created_at": "2026-01-18T11:50:22+00:00",
            "event_created_at": None,
        }
    ]
    assert payload["raw_report"] is report_dict
    assert payload["metadata"] == {
        "event_count": 1,
        "chat_item_count": 4,
        "tool_call_count": 1,
    }
    assert payload["session"]["egress_id"] == "EG_123"
    assert payload["subject"]["resolved_user_id"] == "user-1"


def test_normalize_session_report_includes_compact_tools_without_raw_history() -> None:
    report_dict = {
        "job_id": "AJ_123",
        "room_id": "RM_123",
        "room": "diagnostic_room",
        "started_at": 1_768_737_016.0,
        "timestamp": 1_768_737_276.0,
        "duration": 260.0,
        "chat_history": {
            "items": [
                {
                    "id": "config_1",
                    "type": "agent_config_update",
                    "instructions": "secret system prompt",
                    "tools_added": ["retrieve_knowledge"],
                    "created_at": 1_768_737_019.0,
                },
                {
                    "id": "call_1",
                    "type": "function_call",
                    "call_id": "fc_1",
                    "name": "retrieve_knowledge",
                    "arguments": '{"query":"systems"}',
                    "created_at": 1_768_737_021.0,
                },
                {
                    "id": "out_1",
                    "type": "function_call_output",
                    "call_id": "fc_1",
                    "name": "retrieve_knowledge",
                    "output": '{"status":"ok"}',
                    "is_error": False,
                    "created_at": 1_768_737_022.0,
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": ["Here is the next question."],
                    "created_at": 1_768_737_023.0,
                },
            ]
        },
        "events": [],
    }

    payload = normalize_session_report(
        report_dict,
        agent_type="diagnostic-agent",
        agent_name="diagnostic-agent",
    )

    assert payload["turns"] == [
        {
            "index": 3,
            "role": "assistant",
            "text": "Here is the next question.",
            "timestamp": "2026-01-18T11:50:23+00:00",
        }
    ]
    assert payload["tools"] == {
        "configured": ["retrieve_knowledge"],
        "calls": [
            {
                "call_id": "fc_1",
                "name": "retrieve_knowledge",
                "arguments": '{"query":"systems"}',
                "created_at": "2026-01-18T11:50:21+00:00",
                "output": '{"status":"ok"}',
                "is_error": False,
                "output_created_at": "2026-01-18T11:50:22+00:00",
            }
        ],
    }
    assert "chat_history" not in payload
    assert "events" not in payload
    assert "raw_report" not in payload
    assert payload["metadata"]["tool_call_count"] == 1
