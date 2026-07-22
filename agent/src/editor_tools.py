from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from livekit.agents import function_tool

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = ("java", "javascript", "python")
EDITOR_SURFACES = ("code", "whiteboard")
QuestionStartedCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def _publish_editor_event(room: Any, payload: dict[str, object]) -> None:
    await room.local_participant.publish_data(
        json.dumps(payload).encode("utf-8"),
        reliable=True,
    )
    logger.info("Published %s event", payload.get("type"))


async def _notify_question_started(
    callback: QuestionStartedCallback | None,
    question: dict[str, Any],
) -> None:
    if callback is None:
        return
    try:
        await callback(question)
    except Exception:
        logger.exception("Question-start callback failed question_id=%s", question.get("id"))


def _code_editor_payload(question: str, language: str) -> dict[str, object]:
    return {
        "type": "open_code_editor",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"question": question, "language": language},
    }


def _whiteboard_payload(question: str) -> dict[str, object]:
    return {
        "type": "open_whiteboard",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"question": question},
    }


def _question_started_payload(question: dict[str, Any]) -> dict[str, object]:
    return {
        "type": "interview_question_started",
        "status": "started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {"question": question},
    }


def _normalize_language(value: object) -> str:
    language = value.strip().lower() if isinstance(value, str) else ""
    return language if language in SUPPORTED_LANGUAGES else "javascript"


def _normalize_question(record: object) -> dict[str, Any] | None:
    """Normalize a metadata question for both visual display and spoken output.

    Returns None for records without a usable id/text. Questions without an
    editor surface are kept (surface "verbal") so the tool can report that no
    editor is needed instead of "not found".
    """
    if not isinstance(record, dict):
        return None
    question_id = record.get("id")
    text = record.get("text")
    if not isinstance(question_id, str) or not question_id.strip():
        return None
    if not isinstance(text, str) or not text.strip():
        return None
    surface_raw = record.get("surface")
    surface = surface_raw.strip().lower() if isinstance(surface_raw, str) else "verbal"
    if surface not in EDITOR_SURFACES:
        surface = "verbal"
    spoken_text_raw = record.get("spokenText")
    spoken_text = (
        spoken_text_raw.strip()
        if isinstance(spoken_text_raw, str) and spoken_text_raw.strip()
        else text.strip()
    )
    answer_mode = "surface" if surface in EDITOR_SURFACES else "verbal"
    if surface == "code" and record.get("answerMode") == "verbal":
        answer_mode = "verbal"
    starter_code_raw = record.get("starterCode")
    starter_code = starter_code_raw.strip() if isinstance(starter_code_raw, str) else ""
    return {
        "id": question_id.strip(),
        "text": text.strip(),
        "spokenText": spoken_text,
        "surface": surface,
        "answerMode": answer_mode,
        "language": _normalize_language(record.get("language")),
        "starterCode": starter_code,
    }


def _build_question_editor_tool(
    room: Any,
    questions: list,
    on_question_started: QuestionStartedCallback | None,
) -> tuple:
    by_id: dict[str, dict[str, Any]] = {}
    for record in questions:
        normalized = _normalize_question(record)
        if normalized is not None:
            by_id[normalized["id"]] = normalized

    @function_tool(
        name="mark_question_started",
        description=(
            "Call this immediately before asking a planned verbal interview "
            "question. Pass the question's id shown in the interview plan. It "
            "publishes the full question object to the candidate's screen and "
            "returns the TTS-safe question text to ask. Do not call it for code "
            "viewer, code editor, or whiteboard questions; open_question_editor "
            "handles those."
        ),
    )
    async def mark_question_started(question_id: str) -> dict[str, object]:
        identifier = question_id.strip() if isinstance(question_id, str) else ""
        question = by_id.get(identifier)
        if question is None:
            return {
                "status": "not_found",
                "message": (
                    "Question id was not found. Use one of the ids shown in "
                    "brackets in the interview plan."
                ),
            }
        try:
            await _publish_editor_event(room, _question_started_payload(question))
            await _notify_question_started(on_question_started, question)
        except Exception as e:
            logger.error(f"Failed to publish question started event: {e}")
            return {
                "status": "publish_failed",
                "message": (
                    "The question could not be published to the candidate's screen."
                ),
            }
        return {
            "status": "ok",
            "question_text": question["spokenText"],
            "question": question,
        }

    @function_tool(
        name="open_question_editor",
        description=(
            "Call this immediately before asking an interview question marked "
            "(code viewer), (code editor), or (whiteboard) in your interview "
            "plan. Pass the question's id (shown in brackets in the plan). It opens the right "
            "surface with the full visual question and returns a TTS-safe "
            "question_text plus answer_mode. For answer_mode verbal, ask the "
            "returned text and wait for a spoken answer. For answer_mode surface, "
            "tell the candidate to write or draw their answer and say aloud when "
            "they are done. Do not call it for (verbal) questions."
        ),
    )
    async def open_question_editor(question_id: str) -> dict[str, object]:
        identifier = question_id.strip() if isinstance(question_id, str) else ""
        question = by_id.get(identifier)
        if question is None:
            return {
                "status": "not_found",
                "message": (
                    "Question id was not found. Use one of the ids shown in "
                    "brackets in the interview plan."
                ),
            }
        if question["surface"] == "code":
            payload = _code_editor_payload(question["text"], question["language"])
        elif question["surface"] == "whiteboard":
            payload = _whiteboard_payload(question["text"])
        else:
            return {
                "status": "verbal_only",
                "question_text": question["spokenText"],
                "message": "This question is verbal; no editor is needed. Just ask it.",
            }
        try:
            await _publish_editor_event(room, _question_started_payload(question))
            await _notify_question_started(on_question_started, question)
            await _publish_editor_event(room, payload)
        except Exception as e:
            logger.error(f"Failed to publish editor event: {e}")
            return {
                "status": "publish_failed",
                "message": "The editor could not be opened on the candidate's screen.",
            }
        return {
            "status": "ok",
            "surface": question["surface"],
            "answer_mode": question["answerMode"],
            "question_text": question["spokenText"],
            "question": question,
        }

    return mark_question_started, open_question_editor


def _build_freeform_tools(
    room: Any,
    on_question_started: QuestionStartedCallback | None,
) -> tuple:
    @function_tool(
        name="open_code_editor",
        description=(
            "Open a code editor on the candidate's screen for a coding question. "
            "Call this right before asking the candidate to write code. Pass the "
            "full question text exactly as it should appear on screen, and the "
            "preferred starting language (java, javascript, or python). After "
            "calling it, tell the candidate the editor is open and to say so "
            "when they are done."
        ),
    )
    async def open_code_editor(question: str, language: str) -> dict[str, object]:
        question_text = question.strip() if isinstance(question, str) else ""
        if not question_text:
            return {"status": "error", "message": "question must be a non-empty string."}
        try:
            await _publish_editor_event(
                room, _code_editor_payload(question_text, _normalize_language(language))
            )
            await _notify_question_started(
                on_question_started,
                {
                    "id": question_text,
                    "text": question_text,
                    "surface": "code",
                    "language": _normalize_language(language),
                },
            )
        except Exception as e:
            logger.error(f"Failed to publish open_code_editor event: {e}")
            return {
                "status": "publish_failed",
                "message": "Code editor could not be opened on the candidate's screen.",
            }
        return {"status": "ok", "question": question_text}

    @function_tool(
        name="open_whiteboard",
        description=(
            "Open a whiteboard on the candidate's screen for a design or "
            "diagramming question. Call this right before asking the candidate "
            "to sketch or diagram something. Pass the full question text exactly "
            "as it should appear on screen. After calling it, tell the candidate "
            "the whiteboard is open and to say so when they are done."
        ),
    )
    async def open_whiteboard(question: str) -> dict[str, object]:
        question_text = question.strip() if isinstance(question, str) else ""
        if not question_text:
            return {"status": "error", "message": "question must be a non-empty string."}
        try:
            await _publish_editor_event(room, _whiteboard_payload(question_text))
            await _notify_question_started(
                on_question_started,
                {
                    "id": question_text,
                    "text": question_text,
                    "surface": "whiteboard",
                    "language": "javascript",
                },
            )
        except Exception as e:
            logger.error(f"Failed to publish open_whiteboard event: {e}")
            return {
                "status": "publish_failed",
                "message": "Whiteboard could not be opened on the candidate's screen.",
            }
        return {"status": "ok", "question": question_text}

    return open_code_editor, open_whiteboard


def build_editor_tools(
    room: Any,
    questions: object | None = None,
    on_question_started: QuestionStartedCallback | None = None,
):
    """Tools that open a coding editor / whiteboard on the candidate's screen.

    When room metadata provides a questions list ({id, text, surface,
    language?}), id-based question-start and editor tools are exposed so the
    published question always matches the configured one. Without questions,
    the free-form open_code_editor / open_whiteboard tools are exposed instead.
    Editor content never comes back to the agent; the candidate says out loud
    when they are done.
    """
    if isinstance(questions, list) and questions:
        return _build_question_editor_tool(room, questions, on_question_started)
    return _build_freeform_tools(room, on_question_started)
