from __future__ import annotations

import pytest

from services.agent.unified import SESSION_TIMER_ROLE, UnifiedAgent


@pytest.mark.asyncio
async def test_elapsed_time_context_is_added_as_developer_message() -> None:
    agent = UnifiedAgent(
        instructions="You are a test agent.",
        tools=[],
        initial_reply="Hello.",
    )

    await agent._inject_elapsed_time_context(2)

    messages = agent.chat_ctx.messages()
    assert len(messages) == 1
    assert messages[0].role == SESSION_TIMER_ROLE
    assert messages[0].extra == {
        "internal_timer": True,
        "elapsed_minutes": 2,
    }
    assert "2 minutes elapsed" in (messages[0].text_content or "")
    assert "Do not mention this timing message" in (messages[0].text_content or "")


class _FakeSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def generate_reply(self, **kwargs: object) -> None:
        self.calls.append(kwargs)


async def _first_reply(agent: UnifiedAgent) -> dict[str, object]:
    fake = _FakeSession()
    agent._start_session_timer = lambda: None  # type: ignore[method-assign]
    type(agent).session = property(lambda self: fake)  # type: ignore[assignment]
    try:
        await agent.on_enter()
    finally:
        del type(agent).session
    assert len(fake.calls) == 1
    return fake.calls[0]


class _StarterAgent(UnifiedAgent):
    pass


@pytest.mark.asyncio
async def test_first_reply_uses_starter_question_when_given() -> None:
    agent = _StarterAgent(
        instructions="You are a test agent.",
        tools=[],
        initial_reply="Say the opening.",
        starter="Why don't I delegate?",
        starter_reply="Acknowledge it, then ask their name.",
    )
    call = await _first_reply(agent)
    assert call == {
        "user_input": "Why don't I delegate?",
        "instructions": "Acknowledge it, then ask their name.",
        "allow_interruptions": False,
    }


@pytest.mark.asyncio
async def test_first_reply_ignores_starter_without_starter_reply() -> None:
    agent = _StarterAgent(
        instructions="You are a test agent.",
        tools=[],
        initial_reply="Say the opening.",
        starter="Why don't I delegate?",
    )
    call = await _first_reply(agent)
    assert call == {"instructions": "Say the opening."}
