from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from livekit import rtc

logger = logging.getLogger(__name__)
WHITEBOARD_EVALUATION_TOPIC = "candidate.whiteboard_evaluation"
WHITEBOARD_STATUS_TOPIC = "agent.whiteboard_answer_status"
MAX_WHITEBOARD_BYTES = 4 * 1024 * 1024
MAX_ASSESSMENT_CHARS = 12_000
ASSESSMENT_MAX_AGE_SECONDS = 300
STREAM_LOG_PREFIX = "[STREAM:whiteboard]"
_IMAGE_HASH_RE = re.compile(r"^[a-f0-9]{64}$")


class WhiteboardEvidence:
    """Receive verified visual assessments for browser-persisted whiteboards."""

    def __init__(
        self,
        *,
        participant_identity: str,
        room_name: str,
        on_answer_submitted: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        self._participant_identity = participant_identity
        self._room_name = room_name
        self._on_answer_submitted = on_answer_submitted
        self._questions_by_id: dict[str, dict[str, Any]] = {}
        self._active_question_id: str | None = None
        self._answers: dict[str, dict[str, Any]] = {}
        self._room: rtc.Room | None = None
        self._stream_tasks: set[asyncio.Task[None]] = set()

    def load_plan(self, questions: list[dict[str, Any]]) -> None:
        self._questions_by_id = {
            question["id"]: question
            for question in questions
            if isinstance(question.get("id"), str)
        }
        self._active_question_id = None
        self._answers.clear()

    def on_question_started(self, question_id: str) -> None:
        if question_id in self._questions_by_id:
            self._active_question_id = question_id

    def start(self, room: rtc.Room) -> None:
        room.register_text_stream_handler(
            WHITEBOARD_EVALUATION_TOPIC, self._on_evaluation_stream
        )
        self._room = room

    async def close(self) -> None:
        room = self._room
        self._room = None
        if room is not None:
            try:
                room.unregister_text_stream_handler(WHITEBOARD_EVALUATION_TOPIC)
            except ValueError:
                pass
        await self.wait_for_pending()

    async def wait_for_pending(self) -> None:
        if self._stream_tasks:
            await asyncio.gather(*set(self._stream_tasks), return_exceptions=True)

    def evidence_for(self, question_id: str) -> dict[str, Any] | None:
        answer = self._answers.get(question_id)
        if answer is None:
            return None
        evidence: dict[str, Any] = {
            "submitted": True,
            "revision": answer["revision"],
            "evaluation_status": answer["evaluation_status"],
        }
        if isinstance(answer.get("visual_assessment"), dict):
            evidence["visual_assessment"] = deepcopy(answer["visual_assessment"])
        return evidence

    def has_accepted(self, question_id: str) -> bool:
        return question_id in self._answers

    def active_assessment(self) -> dict[str, Any] | None:
        question_id = self._active_question_id
        if question_id is None:
            return None
        answer = self._answers.get(question_id)
        assessment = answer.get("visual_assessment") if answer is not None else None
        if not isinstance(assessment, dict):
            return None
        return {
            "questionId": question_id,
            **deepcopy(assessment),
        }

    def _track_stream(self, coroutine: Any, *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    def _on_evaluation_stream(
        self,
        reader: rtc.TextStreamReader,
        participant_identity: str,
    ) -> None:
        self._track_stream(
            self._consume_evaluation(reader, participant_identity),
            name="candidate-whiteboard-evaluation",
        )

    async def _publish_status(
        self,
        *,
        question_id: str,
        revision: int,
        status: str,
        message: str | None = None,
    ) -> None:
        room = self._room
        if room is None:
            return
        event: dict[str, Any] = {
            "type": "whiteboard_answer_status",
            "questionId": question_id,
            "revision": revision,
            "status": status,
        }
        if message:
            event["message"] = message
        await room.local_participant.publish_data(
            json.dumps(event, separators=(",", ":")).encode(),
            reliable=True,
            destination_identities=[self._participant_identity],
            topic=WHITEBOARD_STATUS_TOPIC,
        )

    async def _reject(self, question_id: str, revision: int, message: str) -> None:
        logger.warning(
            "%s rejected question_id=%s revision=%s reason=%s",
            STREAM_LOG_PREFIX,
            question_id or "unknown",
            revision,
            message,
        )
        await self._publish_status(
            question_id=question_id,
            revision=revision,
            status="rejected",
            message=message,
        )

    async def _consume_evaluation(
        self,
        reader: rtc.TextStreamReader,
        participant_identity: str,
    ) -> None:
        started = time.monotonic()
        if participant_identity != self._participant_identity:
            return
        logger.info("%s assessment_started", STREAM_LOG_PREFIX)
        raw = await reader.read_all()
        if len(raw) > MAX_ASSESSMENT_CHARS:
            logger.warning("%s ignored oversized visual assessment", STREAM_LOG_PREFIX)
            return
        try:
            wrapper = json.loads(raw)
            payload = wrapper["payload"]
            signature = wrapper["signature"]
            if not all(isinstance(value, str) for value in (payload, signature)):
                raise TypeError
        except (KeyError, TypeError, json.JSONDecodeError):
            logger.warning("%s ignored malformed visual assessment", STREAM_LOG_PREFIX)
            return
        try:
            assessment = json.loads(payload)
            question_id = assessment.get("questionId", "")
            revision = assessment.get("revision", -1)
        except (TypeError, json.JSONDecodeError):
            logger.warning("%s ignored malformed assessment payload", STREAM_LOG_PREFIX)
            return
        if (
            not isinstance(question_id, str)
            or not isinstance(revision, int)
            or isinstance(revision, bool)
        ):
            logger.warning("%s ignored unbound visual assessment", STREAM_LOG_PREFIX)
            return

        secret = os.getenv("WHITEBOARD_EVALUATION_SIGNING_SECRET", "")
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, signature):
            await self._reject(question_id, revision, "Invalid whiteboard assessment signature.")
            return

        question = self._questions_by_id.get(question_id)
        if (
            question is None
            or question_id != self._active_question_id
            or question.get("surface") != "whiteboard"
            or question.get("answerMode") != "surface"
        ):
            await self._reject(question_id, revision, "Whiteboard question is not active.")
            return

        evaluated_at = assessment.get("evaluatedAt")
        image_hash = assessment.get("imageSha256")
        image_bytes = assessment.get("imageBytes")
        s3_key = assessment.get("s3Key")
        evaluation_status = assessment.get("evaluationStatus")
        now_ms = int(time.time() * 1000)
        expected_suffix = (
            f"/whiteboards/{question_id}-{image_hash[:12]}.png"
            if isinstance(image_hash, str)
            else ""
        )
        common_valid = (
            assessment.get("version") == 1
            and assessment.get("roomName") == self._room_name
            and assessment.get("participantIdentity") == self._participant_identity
            and revision >= 0
            and isinstance(image_hash, str)
            and _IMAGE_HASH_RE.fullmatch(image_hash) is not None
            and isinstance(image_bytes, int)
            and not isinstance(image_bytes, bool)
            and 0 < image_bytes <= MAX_WHITEBOARD_BYTES
            and isinstance(s3_key, str)
            and s3_key.endswith(expected_suffix)
            and evaluation_status in {"completed", "failed"}
            and isinstance(evaluated_at, int)
            and not isinstance(evaluated_at, bool)
            and now_ms - ASSESSMENT_MAX_AGE_SECONDS * 1000 <= evaluated_at <= now_ms + 30_000
        )
        completed_valid = evaluation_status != "completed" or (
            isinstance(assessment.get("drawingSummary"), dict)
            and isinstance(assessment.get("visualEvaluation"), dict)
        )
        if not common_valid or not completed_valid:
            await self._reject(question_id, revision, "Invalid whiteboard assessment metadata.")
            return

        previous = self._answers.get(question_id)
        if previous is not None:
            if revision < previous["revision"]:
                await self._reject(question_id, revision, "A newer whiteboard revision exists.")
                return
            if revision == previous["revision"]:
                if image_hash == previous["image_sha256"]:
                    await self._publish_status(
                        question_id=question_id,
                        revision=revision,
                        status="accepted",
                    )
                    return
                await self._reject(question_id, revision, "Revision conflicts with accepted image.")
                return

        if self._on_answer_submitted is not None:
            await self._on_answer_submitted(question_id)
        answer: dict[str, Any] = {
            "revision": revision,
            "image_sha256": image_hash,
            "mime_type": "image/png",
            "bytes": image_bytes,
            "s3_key": s3_key,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "persistence_status": "completed",
            "evaluation_status": evaluation_status,
        }
        if evaluation_status == "completed":
            answer["visual_assessment"] = {
                "drawingSummary": assessment["drawingSummary"],
                "visualEvaluation": assessment["visualEvaluation"],
            }
        self._answers[question_id] = answer
        await self._publish_status(
            question_id=question_id,
            revision=revision,
            status="accepted",
        )
        logger.info(
            "%s accepted question_id=%s revision=%d evaluation_status=%s elapsed_ms=%d",
            STREAM_LOG_PREFIX,
            question_id,
            revision,
            evaluation_status,
            int((time.monotonic() - started) * 1000),
        )
