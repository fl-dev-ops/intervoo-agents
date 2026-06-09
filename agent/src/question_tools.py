from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from livekit.agents import function_tool

logger = logging.getLogger(__name__)


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
    logger.info("Published diagnostic_question_started")


def build_question_event_tool(room: Any | None = None):
    @function_tool(
        name="mark_question_started",
        description=(
            "Call this immediately before asking each provided interview question. "
            "Pass the exact question text you are about to ask. It publishes the "
            "question to the candidate's screen and returns it back. Do not call it "
            "for follow-up probes or for the technical-thinking project discovery "
            "questions."
        ),
    )
    async def mark_question_started(question_text: str) -> dict[str, object]:
        text = question_text.strip() if isinstance(question_text, str) else ""
        if not text:
            return {
                "status": "error",
                "message": "question_text must be a non-empty string.",
            }

        question: dict[str, object] = {"text": text}

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
            "question_text": text,
            "question": question,
        }

    return mark_question_started
