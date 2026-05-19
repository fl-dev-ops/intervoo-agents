from __future__ import annotations

import json

import pytest


class FakeLocalParticipant:
    def __init__(self) -> None:
        self.payloads: list[dict[str, object]] = []

    async def publish_data(self, data: bytes, *, reliable: bool) -> None:
        assert reliable is True
        self.payloads.append(json.loads(data.decode("utf-8")))


class FakeRoom:
    def __init__(self) -> None:
        self.local_participant = FakeLocalParticipant()


def test_diagnostic_kb_tool_exposes_question_started_marker() -> None:
    from kb_tools import build_kb_tool

    tools = build_kb_tool("diagnostic", object())

    assert [tool.id for tool in tools] == ["retrieve_knowledge", "mark_question_started"]


@pytest.mark.asyncio
async def test_retrieve_knowledge_uses_filters_and_marks_question_started(
    monkeypatch,
) -> None:
    from kb_tools import build_kb_tool
    captured: dict[str, object] = {}

    async def fake_retrieve_knowledge_from_base(
        kb,
        *,
        query,
        filters,
        exclude_ids,
        limit,
    ) -> dict[str, object]:
        captured["filters"] = filters
        return {
            "status": "ok",
            "records": [
                {
                    "id": " q1 ",
                    "text": " What is a process? ",
                    "metadata": {
                        "question_type": "Thinking, Language",
                        "category": "domain",
                        "difficulty_level": "easy",
                        "band": "3",
                    },
                }
            ],
        }

    monkeypatch.setattr(
        "kb_tools.retrieve_knowledge_from_base",
        fake_retrieve_knowledge_from_base,
    )
    room = FakeRoom()
    retrieve_knowledge, mark_question_started = build_kb_tool(
        "diagnostic",
        object(),
        room=room,
    )

    await retrieve_knowledge._func(
        "systems",
        content_type="diagnostic_question",
        domain="computer_science",
        category="domain",
        band=3,
    )
    result = await mark_question_started._func("q1")

    assert captured["filters"] == {
        "content_type": "diagnostic_question",
        "domain": "computer_science",
        "category": "domain",
        "band": 3,
    }
    assert result == {
        "status": "ok",
        "question_id": "q1",
        "question_text": "What is a process?",
        "question": {
            "id": "q1",
            "text": "What is a process?",
            "question_type": ["Thinking", "Language"],
            "category": "domain",
            "difficulty_level": "easy",
            "band": 3,
        },
    }
    assert room.local_participant.payloads[-1]["type"] == "diagnostic_question_started"
    assert room.local_participant.payloads[-1]["status"] == "started"
    assert room.local_participant.payloads[-1]["metadata"] == {
        "question": result["question"],
    }
