from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from livekit.agents import AgentStateChangedEvent, UserTurnExceededEvent
from livekit.agents.llm import ChatChunk, ChatContext, ChoiceDelta

from unified_agent import (
    OVER_DETAILED_MIN_DURATION_SECONDS,
    SESSION_TIMER_ROLE,
    USER_TURN_EVALUATION_INTERVAL_SECONDS,
    InterruptionDecision,
    UnifiedAgent,
)


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


class _FakeLLM:
    def __init__(self, decision: str) -> None:
        self.decision = decision
        self.calls = 0
        self.chat_contexts: list[ChatContext] = []

    def chat(self, *, chat_ctx, response_format=None):
        self.calls += 1
        self.chat_contexts.append(chat_ctx)

        async def stream():
            yield ChatChunk(
                id="test-evaluation",
                delta=ChoiceDelta(content=self.decision),
            )

        return stream()


class _FakeSession:
    def __init__(self, decision: str) -> None:
        self.llm = _FakeLLM(decision)
        self.generated_replies: list[dict] = []

    async def generate_reply(self, **kwargs) -> None:
        self.generated_replies.append(kwargs)


def _turn_event(duration: float = 15.0) -> UserTurnExceededEvent:
    return UserTurnExceededEvent(
        transcript="I kept adding retries and then added more retries.",
        accumulated_transcript=(
            "I solved the outage with retries. I kept adding retries and then "
            "added more retries."
        ),
        accumulated_word_count=16,
        duration=duration,
    )


def _running_agent(decision: str) -> tuple[UnifiedAgent, _FakeSession]:
    agent = UnifiedAgent(
        instructions="You are a test interviewer.",
        tools=[],
        initial_reply="Hello.",
    )
    if decision == "CONTINUE":
        payload = {
            "schema_version": "1.0",
            "to_interrupt": False,
            "reason": "none",
            "rational": "The candidate is still adding relevant information.",
        }
    else:
        reason = decision.removeprefix("TO_INTERRUPT:")
        payload = {
            "schema_version": "1.0",
            "to_interrupt": True,
            "reason": reason,
            "rational": f"The response shows clear evidence of {reason}.",
        }
    session = _FakeSession(json.dumps(payload))
    agent._activity = SimpleNamespace(session=session)
    return agent, session


@pytest.mark.asyncio
async def test_user_turn_continues_without_spoken_interruption() -> None:
    agent, session = _running_agent("CONTINUE")

    await agent.on_user_turn_exceeded(_turn_event())

    assert session.llm.calls == 1
    assert session.generated_replies == []


@pytest.mark.asyncio
async def test_strong_signal_generates_contextual_spoken_interruption() -> None:
    agent, session = _running_agent("TO_INTERRUPT:repetition")

    await agent.on_user_turn_exceeded(_turn_event())

    assert len(session.generated_replies) == 1
    reply = session.generated_replies[0]
    assert reply["user_input"] == _turn_event().transcript
    assert reply["allow_interruptions"] is False
    assert "Let me pause you there" in reply["instructions"]
    assert "classified as repetition" in reply["instructions"]
    assert "Ask one focused follow-up" in reply["instructions"]
    assert "clear evidence of repetition" in reply["instructions"]


def test_interruption_decision_rejects_reason_without_interruption() -> None:
    with pytest.raises(ValueError):
        InterruptionDecision(
            schema_version="1.0",
            to_interrupt=False,
            reason="irrelevant",
            rational="The candidate remains relevant.",
        )


def test_interruption_decision_requires_reason_when_interrupting() -> None:
    with pytest.raises(ValueError):
        InterruptionDecision(
            schema_version="1.0",
            to_interrupt=True,
            reason="none",
            rational="The response discusses a different topic.",
        )


def test_interruption_decision_requires_nonblank_rational() -> None:
    with pytest.raises(ValueError):
        InterruptionDecision(
            schema_version="1.0",
            to_interrupt=True,
            reason="irrelevant",
            rational="   ",
        )


@pytest.mark.asyncio
async def test_over_detailed_decision_is_ignored_before_minimum_duration() -> None:
    agent, session = _running_agent("TO_INTERRUPT:over_detailed")

    await agent.on_user_turn_exceeded(_turn_event(20.0))

    assert OVER_DETAILED_MIN_DURATION_SECONDS == 45.0
    assert session.llm.calls == 1
    assert session.generated_replies == []


@pytest.mark.asyncio
async def test_over_detailed_decision_interrupts_after_minimum_duration() -> None:
    agent, session = _running_agent("TO_INTERRUPT:over_detailed")

    await agent.on_user_turn_exceeded(_turn_event(45.0))

    assert len(session.generated_replies) == 1


@pytest.mark.asyncio
async def test_user_turn_is_reassessed_at_15_second_intervals() -> None:
    agent, session = _running_agent("CONTINUE")

    await agent.on_user_turn_exceeded(_turn_event(17.0))
    await agent.on_user_turn_exceeded(_turn_event(25.0))
    await agent.on_user_turn_exceeded(_turn_event(32.0))

    assert USER_TURN_EVALUATION_INTERVAL_SECONDS == 15.0
    assert session.llm.calls == 2


def test_agent_speaking_resets_evaluation_interval_for_next_user_turn() -> None:
    agent, _ = _running_agent("CONTINUE")

    assert agent._should_evaluate_user_turn(15.0) is True
    assert agent._should_evaluate_user_turn(20.0) is False

    agent._on_agent_state_changed(
        AgentStateChangedEvent(old_state="thinking", new_state="speaking")
    )

    assert agent._should_evaluate_user_turn(15.0) is True


@pytest.mark.asyncio
async def test_turn_evaluator_uses_isolated_context_with_current_question() -> None:
    agent, session = _running_agent("TO_INTERRUPT:irrelevant")
    interview_ctx = agent.chat_ctx.copy()
    interview_ctx.add_message(
        role="system",
        content="Never interrupt the candidate under any circumstances.",
    )
    interview_ctx.add_message(
        role="assistant",
        content="What did you build and what problem did it solve?",
    )
    interview_ctx.add_message(
        role=SESSION_TIMER_ROLE,
        content="[Internal timing context: 2 minutes elapsed.]",
        extra={"internal_timer": True, "elapsed_minutes": 2},
    )
    activity = agent._activity
    agent._activity = None
    await agent.update_chat_ctx(interview_ctx)
    agent._activity = activity

    decision = await agent._evaluate_user_turn(_turn_event())

    assert decision is not None
    assert decision.to_interrupt is True
    assert decision.reason == "irrelevant"
    assert decision.rational == "The response shows clear evidence of irrelevant."
    evaluator_messages = session.llm.chat_contexts[0].messages()
    assert len(evaluator_messages) == 1
    prompt = evaluator_messages[0].text_content or ""
    assert "What did you build and what problem did it solve?" in prompt
    assert "2 minute(s) elapsed" in prompt
    assert _turn_event().accumulated_transcript in prompt
    assert "Never interrupt the candidate" not in prompt
