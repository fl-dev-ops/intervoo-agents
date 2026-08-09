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

from interview.whiteboard_store import WhiteboardArtifactStore
from recording_config import RecordingConfig

logger = logging.getLogger(__name__)
WHITEBOARD_ANSWER_TOPIC = "candidate.whiteboard_answer"
WHITEBOARD_EVALUATION_TOPIC = "candidate.whiteboard_evaluation"
WHITEBOARD_STATUS_TOPIC = "agent.whiteboard_answer_status"
MAX_WHITEBOARD_BYTES = 4 * 1024 * 1024
MAX_ASSESSMENT_CHARS = 12_000
ASSESSMENT_MAX_AGE_SECONDS = 300
ASSESSMENT_DRAIN_TIMEOUT_SECONDS = 5
STREAM_LOG_PREFIX = "[STREAM:whiteboard]"
_FILENAME_RE = re.compile(r"^whiteboard\.(\d+)\.([a-f0-9]{64})\.png$")

class WhiteboardEvidence:
    """Receive final whiteboard images and verified compact visual assessments."""

    def __init__(
        self,
        *,
        participant_identity: str,
        room_name: str,
        agent_type: str,
        recording_config: RecordingConfig | None,
        on_answer_submitted: Callable[[str], Awaitable[None]] | None,
    ) -> None:
        self._participant_identity = participant_identity
        self._room_name = room_name
        self._recording_config = recording_config
        self._on_answer_submitted = on_answer_submitted
        self._questions_by_id: dict[str, dict[str, Any]] = {}
        self._active_question_id: str | None = None
        self._answers: dict[str, dict[str, Any]] = {}
        self._room: rtc.Room | None = None
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._background_tasks: set[asyncio.Task[None]] = set()
        self._assessment_events: dict[str, asyncio.Event] = {}
        self._artifact_store = (
            WhiteboardArtifactStore(
                config=recording_config,
                agent_type=agent_type,
                room_name=room_name,
                answers=self._answers,
            )
            if recording_config is not None
            else None
        )

    def load_plan(self, questions: list[dict[str, Any]]) -> None:
        self._questions_by_id = {
            question["id"]: question
            for question in questions
            if isinstance(question.get("id"), str)
        }
        self._active_question_id = None
        self._answers.clear()
        self._assessment_events.clear()

    def on_question_started(self, question_id: str) -> None:
        if question_id in self._questions_by_id:
            self._active_question_id = question_id

    def start(self, room: rtc.Room) -> None:
        room.register_byte_stream_handler(WHITEBOARD_ANSWER_TOPIC, self._on_image_stream)
        room.register_text_stream_handler(
            WHITEBOARD_EVALUATION_TOPIC, self._on_evaluation_stream
        )
        self._room = room

    async def close(self) -> None:
        room = self._room
        self._room = None
        if room is not None:
            for unregister, topic in (
                (room.unregister_byte_stream_handler, WHITEBOARD_ANSWER_TOPIC),
                (room.unregister_text_stream_handler, WHITEBOARD_EVALUATION_TOPIC),
            ):
                try:
                    unregister(topic)
                except ValueError:
                    pass
        await self.wait_for_pending()
        if self._background_tasks:
            await asyncio.gather(*set(self._background_tasks), return_exceptions=True)
        for answer in self._answers.values():
            answer.pop("_image_bytes", None)

    async def wait_for_pending(self) -> None:
        if self._stream_tasks:
            await asyncio.gather(*set(self._stream_tasks), return_exceptions=True)
        pending = [
            event.wait()
            for question_id, event in self._assessment_events.items()
            if self._answers.get(question_id, {}).get("evaluation_status") == "pending"
        ]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending),
                    timeout=ASSESSMENT_DRAIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "%s visual assessment wait timed out after %ss",
                    STREAM_LOG_PREFIX,
                    ASSESSMENT_DRAIN_TIMEOUT_SECONDS,
                )

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

    def _track_stream(self, coroutine: Any, *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    def _track_background(self, coroutine: Any, *, name: str) -> None:
        task = asyncio.create_task(coroutine, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _on_image_stream(
        self,
        reader: rtc.ByteStreamReader,
        participant_identity: str,
    ) -> None:
        self._track_stream(
            self._consume_image(reader, participant_identity),
            name="candidate-whiteboard-image",
        )

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

    async def _consume_image(
        self,
        reader: rtc.ByteStreamReader,
        participant_identity: str,
    ) -> None:
        started = time.monotonic()
        info = reader.info
        attributes = info.attributes or {}
        has_contract_attributes = all(
            key in attributes
            for key in ("question_id", "revision", "image_sha256", "submitted")
        )
        filename_match = _FILENAME_RE.fullmatch(info.name or "")
        question_id = (
            attributes["question_id"]
            if has_contract_attributes
            else self._active_question_id or ""
        )
        revision_value = (
            attributes["revision"]
            if has_contract_attributes
            else filename_match.group(1) if filename_match else "-1"
        )
        expected_hash = (
            attributes["image_sha256"]
            if has_contract_attributes
            else filename_match.group(2) if filename_match else ""
        )
        try:
            revision = int(revision_value or "-1")
        except ValueError:
            revision = -1

        if participant_identity != self._participant_identity:
            await self._reject(question_id, revision, "Unexpected participant.")
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
        if (
            info.mime_type != "image/png"
            or info.size is None
            or info.size <= 0
            or info.size > MAX_WHITEBOARD_BYTES
            or revision < 0
            or (
                has_contract_attributes
                and attributes.get("submitted") != "true"
            )
            or (not has_contract_attributes and filename_match is None)
        ):
            await self._reject(question_id, revision, "Invalid whiteboard image metadata.")
            return

        chunks: list[bytes] = []
        byte_count = 0
        async for chunk in reader:
            byte_count += len(chunk)
            if byte_count > MAX_WHITEBOARD_BYTES:
                await self._reject(question_id, revision, "Whiteboard image is too large.")
                return
            chunks.append(chunk)
        image_bytes = b"".join(chunks)
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        if byte_count != info.size or image_hash != expected_hash or not image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            await self._reject(question_id, revision, "Whiteboard image integrity check failed.")
            return

        previous = self._answers.get(question_id)
        if previous is not None:
            previous_revision = previous["revision"]
            if revision < previous_revision:
                await self._reject(question_id, revision, "A newer whiteboard revision exists.")
                return
            if revision == previous_revision:
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
        self._answers[question_id] = {
            "revision": revision,
            "image_sha256": image_hash,
            "mime_type": "image/png",
            "bytes": byte_count,
            "accepted_at": datetime.now(timezone.utc).isoformat(),
            "persistence_status": "pending" if self._recording_config else "disabled",
            "evaluation_status": "pending",
            "_image_bytes": image_bytes,
        }
        self._assessment_events[question_id] = asyncio.Event()
        await self._publish_status(
            question_id=question_id,
            revision=revision,
            status="accepted",
        )
        logger.info(
            "%s accepted question_id=%s revision=%d bytes=%d elapsed_ms=%d",
            STREAM_LOG_PREFIX,
            question_id,
            revision,
            byte_count,
            int((time.monotonic() - started) * 1000),
        )
        if self._artifact_store is not None:
            self._track_background(
                self._artifact_store.persist_image(question_id),
                name=f"persist-whiteboard:{question_id}",
            )

    async def _consume_evaluation(
        self,
        reader: rtc.TextStreamReader,
        participant_identity: str,
    ) -> None:
        if participant_identity != self._participant_identity:
            return
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
        secret = os.getenv("WHITEBOARD_EVALUATION_SIGNING_SECRET", "")
        expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not secret or not hmac.compare_digest(expected, signature):
            logger.warning("%s ignored invalid visual assessment signature", STREAM_LOG_PREFIX)
            return
        try:
            assessment = json.loads(payload)
            question_id = assessment["questionId"]
            answer = self._answers[question_id]
            evaluated_at = int(assessment["evaluatedAt"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("%s ignored unbound visual assessment", STREAM_LOG_PREFIX)
            return
        now_ms = int(time.time() * 1000)
        valid = (
            assessment.get("version") == 1
            and assessment.get("roomName") == self._room_name
            and assessment.get("participantIdentity") == self._participant_identity
            and assessment.get("revision") == answer["revision"]
            and assessment.get("imageSha256") == answer["image_sha256"]
            and answer.get("evaluation_status") == "pending"
            and now_ms - ASSESSMENT_MAX_AGE_SECONDS * 1000 <= evaluated_at <= now_ms + 30_000
            and isinstance(assessment.get("drawingSummary"), dict)
            and isinstance(assessment.get("visualEvaluation"), dict)
        )
        if not valid:
            logger.warning("%s ignored mismatched visual assessment", STREAM_LOG_PREFIX)
            return
        answer["visual_assessment"] = {
            "drawingSummary": assessment["drawingSummary"],
            "visualEvaluation": assessment["visualEvaluation"],
        }
        answer["evaluation_status"] = "completed"
        self._assessment_events[question_id].set()
        logger.info(
            "%s visual assessment accepted question_id=%s revision=%d",
            STREAM_LOG_PREFIX,
            question_id,
            answer["revision"],
        )
        if self._artifact_store is not None:
            self._track_background(
                self._artifact_store.persist_manifest(),
                name=f"persist-whiteboard-manifest:{question_id}",
            )
