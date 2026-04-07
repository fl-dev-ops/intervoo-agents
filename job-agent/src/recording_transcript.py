from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"


def _ts_to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif hasattr(item, "text"):
                parts.append(item.text)
        return " ".join(parts)
    return str(content) if content else ""


def _chat_history_items(report_dict: dict[str, Any]) -> list[dict[str, Any]]:
    chat_history = report_dict.get("chat_history", {})
    if not isinstance(chat_history, dict):
        return []

    raw_items = chat_history.get("items")
    if isinstance(raw_items, list):
        return [item for item in raw_items if isinstance(item, dict)]

    raw_messages = chat_history.get("messages")
    if isinstance(raw_messages, list):
        return [item for item in raw_messages if isinstance(item, dict)]

    return []


def normalize_session_report(
    report_dict: dict[str, Any],
    *,
    agent_type: str,
    agent_name: str,
    egress_id: str | None = None,
    egress_status: str | None = None,
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
) -> dict[str, Any]:
    session_started = report_dict.get("started_at")
    session_timestamp = report_dict.get("timestamp", time.time())
    duration = report_dict.get("duration")

    turns = []
    messages = _chat_history_items(report_dict)

    for idx, msg in enumerate(messages):
        if msg.get("type") not in (None, "message"):
            continue

        role = msg.get("role", "unknown")
        text = _extract_text(msg.get("content", ""))
        if not text:
            continue

        turn: dict[str, Any] = {
            "index": idx,
            "role": role,
            "text": text,
        }

        create_ts = msg.get("create_time") or msg.get("created_at")
        if create_ts:
            turn["timestamp"] = _ts_to_iso(create_ts)

        if msg.get("interrupted"):
            turn["interrupted"] = True

        tool_name = msg.get("tool_name")
        if tool_name:
            turn["tool_name"] = tool_name

        turns.append(turn)

    usage: dict[str, Any] = {}
    options = report_dict.get("options", {})
    if options:
        usage["model"] = options.get("llm", {}).get("model")

    events = report_dict.get("events", [])
    metadata_events = []
    for ev in events:
        if isinstance(ev, dict):
            metadata_events.append(
                {k: v for k, v in ev.items() if k in ("type", "timestamp")}
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "session": {
            "agent_type": agent_type,
            "agent_name": agent_name,
            "room": report_dict.get("room"),
            "room_id": report_dict.get("room_id"),
            "job_id": report_dict.get("job_id"),
            "egress_id": egress_id,
            "egress_status": egress_status,
            "started_at": _ts_to_iso(session_started),
            "ended_at": _ts_to_iso(session_timestamp),
            "duration_seconds": round(duration, 2) if duration else None,
        },
        "subject": {
            "resolved_user_id": resolved_user_id,
            "participant_identity": participant_identity,
            "phone_number": phone_number,
        },
        "turns": turns,
        "usage": usage,
        "metadata": {
            "event_count": len(events),
            "turn_count": len(turns),
            "events_summary": metadata_events[:20],
        },
    }
