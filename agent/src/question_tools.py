from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from livekit.agents import function_tool

logger = logging.getLogger(__name__)


def _normalize_question_type(value: object) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def _normalize_question(record: object) -> dict[str, object] | None:
    """Normalize a provided question object into the full publish shape.

    Returns None when the record lacks a usable id/text/question_type.
    """
    if not isinstance(record, dict):
        return None

    question_id = record.get("id")
    question_text = record.get("text")
    if not isinstance(question_id, str) or not isinstance(question_text, str):
        return None
    if not question_id.strip() or not question_text.strip():
        return None

    question_type = _normalize_question_type(record.get("question_type"))
    if not question_type:
        return None

    band = record.get("band")

    return {
        "id": question_id.strip(),
        "text": question_text.strip(),
        "question_type": question_type,
        "category": record.get("category"),
        "difficulty_level": record.get("difficulty_level"),
        "band": band if isinstance(band, int) else None,
    }


async def _publish_question_started_event(
    room: Any,
    *,
    question: dict[str, object],
) -> None:
    payload = {
        "type": "diagnostic_question_started",
        "status": "started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "question": question,
        },
    }

    await room.local_participant.publish_data(
        json.dumps(payload).encode("utf-8"),
        reliable=True,
    )
    logger.info(
        "Published diagnostic_question_started for %s",
        question.get("id") or question.get("text"),
    )


def build_question_event_tool(
    room: Any | None = None,
    questions: object | None = None,
):
    # Build an id -> full-question lookup from the provided questions. Callers pass
    # the round's questions as objects ({id, text, question_type, ...}); the tool
    # resolves the id the LLM passes and publishes the full question metadata.
    by_id: dict[str, dict[str, object]] = {}
    if isinstance(questions, list):
        for record in questions:
            normalized = _normalize_question(record)
            if normalized is not None:
                by_id[str(normalized["id"])] = normalized

    @function_tool(
        name="mark_question_started",
        description=(
            "Call this immediately before asking each provided interview question. "
            "Pass the question's id (the value shown in brackets next to the "
            "question). It publishes the question to the candidate's screen and "
            "returns it back. Do not call it for follow-up probes or for the "
            "technical-thinking project discovery questions."
        ),
    )
    async def mark_question_started(question_id: str) -> dict[str, object]:
        identifier = question_id.strip() if isinstance(question_id, str) else ""
        if not identifier:
            return {
                "status": "error",
                "message": "question_id must be a non-empty string.",
            }

        question = by_id.get(identifier)
        if question is None:
            return {
                "status": "not_found",
                "message": (
                    "Question id was not found in the provided questions. Use one "
                    "of the ids shown in brackets next to the questions."
                ),
            }

        if room is not None:
            try:
                await _publish_question_started_event(room, question=question)
            except Exception as e:
                logger.error(f"Failed to publish question started event: {e}")
                return {
                    "status": "publish_failed",
                    "question": question,
                    "message": "Question could not be published to the frontend.",
                }

        return {
            "status": "ok",
            "question_text": question["text"],
            "question": question,
        }

    return mark_question_started
