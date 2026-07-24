from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from editor_tools import build_editor_tools


@pytest.mark.asyncio
async def test_verbal_question_tool_publishes_and_speaks_once() -> None:
    published: list[dict[str, object]] = []
    started: list[dict[str, object]] = []
    spoken: list[str] = []

    class FakeLocalParticipant:
        async def publish_data(self, payload: bytes, *, reliable: bool) -> None:
            assert reliable is True
            published.append(json.loads(payload))

    class FakeSession:
        async def say(self, text: str) -> None:
            spoken.append(text)

    async def on_question_started(question: dict[str, object]) -> None:
        started.append(question)

    room = SimpleNamespace(local_participant=FakeLocalParticipant())
    mark_question_started, _ = build_editor_tools(
        room,
        questions=[
            {
                "id": "q1",
                "text": "Explain the event loop.",
                "spokenText": "Explain the event loop.",
                "surface": "verbal",
            }
        ],
        on_question_started=on_question_started,
    )

    result = await mark_question_started._func(
        SimpleNamespace(session=FakeSession()),
        "q1",
    )

    assert result is None
    assert spoken == ["Explain the event loop."]
    assert started[0]["id"] == "q1"
    assert published[0]["type"] == "interview_question_started"


def test_interviewer_prompt_does_not_repeat_tool_spoken_question() -> None:
    prompt = Path(__file__).parents[1] / "prompts/interview/vasanth.md"
    text = prompt.read_text(encoding="utf-8")

    assert "call mark_question_started with its id as a silent tool-only action" in text
    assert "The tool itself speaks the exact TTS-safe question once" in text
    assert "never repeat the question" in text
