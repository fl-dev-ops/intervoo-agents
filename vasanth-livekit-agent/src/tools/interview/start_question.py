from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from livekit.agents import RunContext, function_tool

from interview.question_store import (
    QuestionStore,
    QuestionStoreError,
    candidate_safe_question,
)

logger = logging.getLogger(__name__)

QuestionStartedCallback = Callable[[dict[str, Any]], Awaitable[None]]


def _question_started_payload(question: dict[str, Any]) -> dict[str, object]:
    return {
        "type": "interview_question_started",
        "status": "started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"question": question},
    }


def build_start_question_tool(
    *,
    room: Any,
    question_store: QuestionStore,
    on_question_started: QuestionStartedCallback | None = None,
):
    """Build the only tool that starts a question from the active plan."""

    @function_tool(
        name="start_question",
        description=(
            "Start the next question from the interview plan. Pass its exact id. "
            "Use this for every planned verbal, code-output, coding, machine-coding, "
            "MCQ, or whiteboard question. The tool presents the complete question on "
            "the correct candidate surface and speaks its TTS-safe wording exactly "
            "once. Call it as a silent tool-only action: do not say the question or "
            "an acknowledgement before or after the call. A status of answer_pending "
            "means the previous written question was never submitted: ask the "
            "candidate whether they have finished and submitted it, and only when "
            "they say they cannot finish, call this again with "
            "previous_question_abandoned set to true. Never set that flag while the "
            "candidate is still working."
        ),
    )
    async def start_question(
        context: RunContext,
        question_id: str,
        previous_question_abandoned: bool = False,
    ) -> dict[str, object] | None:
        try:
            internal_question = question_store.reserve_next(
                question_id,
                previous_question_abandoned=previous_question_abandoned,
            )
        except QuestionStoreError as exc:
            return {"status": exc.status, "message": exc.message}

        public_question = candidate_safe_question(internal_question)
        try:
            await room.local_participant.publish_data(
                json.dumps(_question_started_payload(public_question)).encode("utf-8"),
                reliable=True,
            )
        except Exception:
            question_store.mark_delivery_failed(internal_question["id"])
            logger.exception(
                "Failed to publish planned question question_id=%s",
                internal_question["id"],
            )
            return {
                "status": "start_failed",
                "message": "The planned question could not be presented.",
            }

        question_store.mark_started(internal_question["id"])
        try:
            if on_question_started is not None:
                try:
                    await on_question_started(public_question)
                except Exception:
                    logger.exception(
                        "Question-start callback failed question_id=%s",
                        internal_question["id"],
                    )
            speech_handle = context.session.say(internal_question["spokenText"])
            await speech_handle
        except Exception:
            logger.exception(
                "Failed to speak planned question question_id=%s",
                internal_question["id"],
            )
            return {
                "status": "start_failed",
                "message": "The planned question could not be presented.",
            }
        return None

    return start_question
