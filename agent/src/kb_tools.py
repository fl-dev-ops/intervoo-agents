from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from livekit.agents import function_tool

from knowledge_base import ChromaKnowledgeBase, retrieve_knowledge_from_base

logger = logging.getLogger(__name__)


def _normalize_question_type(value: object) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _normalize_question_record(record: dict[str, object]) -> dict[str, object] | None:
    metadata = (
        record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    )
    question_type = _normalize_question_type(metadata.get("question_type"))
    if not question_type:
        question_type_json = metadata.get("question_type_json")
        if isinstance(question_type_json, str):
            try:
                question_type = _normalize_question_type(json.loads(question_type_json))
            except json.JSONDecodeError:
                question_type = []

    question_id = record.get("id")
    question_text = record.get("text")
    if not isinstance(question_id, str) or not isinstance(question_text, str):
        return None
    if not question_id.strip() or not question_text.strip() or not question_type:
        return None

    band = metadata.get("band")
    if isinstance(band, str):
        try:
            band = int(band)
        except ValueError:
            band = None

    return {
        "id": question_id.strip(),
        "text": question_text.strip(),
        "question_type": question_type,
        "category": metadata.get("category"),
        "difficulty_level": metadata.get("difficulty_level"),
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
    logger.info("Published diagnostic_question_started for %s", question["id"])


def make_simple_retrieve_knowledge(kb: ChromaKnowledgeBase):
    @function_tool(
        name="retrieve_knowledge",
        description="Retrieve relevant records from the configured knowledge base.",
    )
    async def retrieve_knowledge(
        query: str,
        filters: dict[str, object] | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        return await retrieve_knowledge_from_base(
            kb,
            query=query,
            filters=filters,
            exclude_ids=exclude_ids,
            limit=limit,
        )

    return retrieve_knowledge


def make_diagnostic_retrieve_knowledge(
    kb: ChromaKnowledgeBase,
    room: Any | None = None,
):
    questions_by_id: dict[str, dict[str, object]] = {}

    @function_tool(
        name="retrieve_knowledge",
        description=(
            "Retrieve relevant records from the configured knowledge base. For this "
            "diagnostic agent, records are assessment questions. Use filters for "
            "stage, domain, difficulty, band, and content_type when known. Only ask "
            "questions returned by this tool."
        ),
    )
    async def retrieve_knowledge(
        query: str,
        content_type: str | None = None,
        domain: str | None = None,
        category: str | None = None,
        difficulty_level: str | list[str] | None = None,
        band: int | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        filters = {
            key: value
            for key, value in {
                "content_type": content_type,
                "domain": domain,
                "category": category,
                "difficulty_level": difficulty_level,
                "band": band,
            }.items()
            if value is not None
        }
        logger.info(f"[KB] Retrieving knowledge: {query=}, {filters=}")
        result = await retrieve_knowledge_from_base(
            kb,
            query=query,
            filters=filters or None,
            exclude_ids=exclude_ids,
            limit=limit,
        )

        records = result.get("records") if isinstance(result, dict) else None
        if result.get("status") == "ok" and isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                question = _normalize_question_record(record)
                if question is not None:
                    questions_by_id[str(question["id"])] = question

        return result

    @function_tool(
        name="mark_question_started",
        description=(
            "Call this immediately before asking any diagnostic question returned "
            "by retrieve_knowledge. It publishes the full question metadata to the "
            "frontend and returns the exact question text to ask."
        ),
    )
    async def mark_question_started(question_id: str) -> dict[str, object]:
        normalized_id = question_id.strip() if isinstance(question_id, str) else ""
        question = questions_by_id.get(normalized_id)
        if question is None:
            return {
                "status": "not_found",
                "message": (
                    "Question id was not found in retrieved records. Call "
                    "retrieve_knowledge first and use one of its returned ids."
                ),
            }

        if room is not None:
            try:
                await _publish_question_started_event(room, question=question)
            except Exception as e:
                logger.error(f"Failed to publish question started event: {e}")
                return {
                    "status": "publish_failed",
                    "question_id": normalized_id,
                    "message": "Question could not be published to the frontend.",
                }

        return {
            "status": "ok",
            "question_id": normalized_id,
            "question_text": question["text"],
            "question": question,
        }

    return retrieve_knowledge, mark_question_started


def make_diagnostic_retrieve_and_start(
    kb: ChromaKnowledgeBase,
    room: Any | None = None,
):
    """Merged tool: retrieves KB questions AND marks the selected question as started.

    This eliminates one LLM round-trip per question by combining retrieve_knowledge
    and mark_question_started into a single tool call. The LLM calls this with the
    query and the question_id it wants to ask — the tool retrieves, publishes the
    event, and returns the question text ready to speak.
    """
    questions_by_id: dict[str, dict[str, object]] = {}

    @function_tool(
        name="retrieve_and_start_question",
        description=(
            "Retrieve a question from the knowledge base AND mark it as started "
            "(publishes to the candidate's screen). Use this instead of calling "
            "retrieve_knowledge and mark_question_started separately. Pass the "
            "question_id of the record you want to ask from the previous retrieve "
            "results, or omit it to just retrieve without starting."
        ),
    )
    async def retrieve_and_start_question(
        query: str,
        question_id: str | None = None,
        content_type: str | None = None,
        domain: str | None = None,
        category: str | None = None,
        difficulty_level: str | list[str] | None = None,
        band: int | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        filters = {
            key: value
            for key, value in {
                "content_type": content_type,
                "domain": domain,
                "category": category,
                "difficulty_level": difficulty_level,
                "band": band,
            }.items()
            if value is not None
        }
        logger.info("[KB] retrieve_and_start_question: query=%s, filters=%s, question_id=%s", query, filters, question_id)

        result = await retrieve_knowledge_from_base(
            kb,
            query=query,
            filters=filters or None,
            exclude_ids=exclude_ids,
            limit=limit,
        )

        records = result.get("records") if isinstance(result, dict) else None
        if result.get("status") == "ok" and isinstance(records, list):
            for record in records:
                if not isinstance(record, dict):
                    continue
                question = _normalize_question_record(record)
                if question is not None:
                    questions_by_id[str(question["id"])] = question

        if question_id is None:
            return result

        normalized_id = question_id.strip()
        question = questions_by_id.get(normalized_id)
        if question is None:
            return {
                "status": "not_found",
                "message": (
                    "Question id not found in retrieved records. "
                    "Pass question_id from a record returned by this same call."
                ),
                "retrieval": result,
            }

        if room is not None:
            try:
                await _publish_question_started_event(room, question=question)
            except Exception as e:
                logger.error("Failed to publish question started event: %s", e)
                return {
                    "status": "publish_failed",
                    "question_id": normalized_id,
                    "message": "Question could not be published to the frontend.",
                    "retrieval": result,
                }

        return {
            "status": "ok",
            "question_id": normalized_id,
            "question_text": question["text"],
            "question": question,
            "retrieval": result,
        }

    return retrieve_and_start_question


def build_kb_tool(
    shape: str,
    kb: ChromaKnowledgeBase,
    room: Any | None = None,
):
    if shape == "simple":
        return make_simple_retrieve_knowledge(kb)
    if shape == "diagnostic":
        return make_diagnostic_retrieve_knowledge(kb, room)
    if shape == "diagnostic_fast":
        return make_diagnostic_retrieve_and_start(kb, room)
    raise ValueError(f"Unknown kb shape: {shape!r}")
