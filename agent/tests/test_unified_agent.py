from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from livekit.agents import (
    AgentStateChangedEvent,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
)
from livekit.agents.llm import ChatChunk, ChatContext, ChoiceDelta

from unified_agent import (
    OVER_DETAILED_MIN_DURATION_SECONDS,
    SESSION_TIMER_ROLE,
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
        self.committed_turns: list[dict] = []
        self.said: list[tuple[str, dict]] = []

    async def generate_reply(self, **kwargs) -> None:
        self.generated_replies.append(kwargs)

    async def commit_user_turn(self, **kwargs) -> str:
        self.committed_turns.append(kwargs)
        return "committed transcript"

    def say(self, text: str, **kwargs) -> None:
        self.said.append((text, kwargs))


def _running_agent(decision: str) -> tuple[UnifiedAgent, _FakeSession]:
    agent = UnifiedAgent(
        instructions="You are a test interviewer.",
        tools=[],
        initial_reply="Hello.",
        manage_candidate_turns=True,
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
    agent._user_speaking = True
    agent._user_turn_generation = 1
    return agent, session


@pytest.mark.asyncio
async def test_user_turn_continues_without_spoken_interruption() -> None:
    agent, session = _running_agent("CONTINUE")

    interrupted = await agent._evaluate_and_maybe_interrupt(
        "I solved the outage with retries and monitoring.", 15.0, 1
    )

    assert interrupted is False
    assert session.llm.calls == 1
    assert session.generated_replies == []


@pytest.mark.asyncio
async def test_strong_signal_generates_contextual_spoken_interruption() -> None:
    agent, session = _running_agent("TO_INTERRUPT:repetition")

    interrupted = await agent._evaluate_and_maybe_interrupt(
        "I kept adding retries and then added more retries.", 15.0, 1
    )

    assert interrupted is True
    assert session.committed_turns == [
        {
            "transcript_timeout": 1.5,
            "stt_flush_duration": 0.2,
            "skip_reply": True,
        }
    ]
    assert len(session.generated_replies) == 1
    reply = session.generated_replies[0]
    assert "user_input" not in reply
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

    await agent._evaluate_and_maybe_interrupt(
        "A detailed but still ongoing answer with enough words.", 20.0, 1
    )

    assert OVER_DETAILED_MIN_DURATION_SECONDS == 45.0
    assert session.llm.calls == 1
    assert session.generated_replies == []


@pytest.mark.asyncio
async def test_over_detailed_decision_interrupts_after_minimum_duration() -> None:
    agent, session = _running_agent("TO_INTERRUPT:over_detailed")

    await agent._evaluate_and_maybe_interrupt(
        "A detailed answer that has now continued for long enough.", 45.0, 1
    )

    assert len(session.generated_replies) == 1


@pytest.mark.asyncio
async def test_user_turn_is_reassessed_at_fixed_intervals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent, session = _running_agent("CONTINUE")
    calls: list[tuple[str, float, int]] = []

    async def evaluate(transcript: str, duration: float, generation: int) -> bool:
        calls.append((transcript, duration, generation))
        if len(calls) == 1:
            agent._latest_user_transcript += " with additional useful details"
        else:
            agent._user_speaking = False
        return False

    monkeypatch.setattr("unified_agent.USER_TURN_EVALUATION_INTERVAL_SECONDS", 0.001)
    monkeypatch.setattr(agent, "_evaluate_and_maybe_interrupt", evaluate)
    agent._latest_user_transcript = "This transcript contains enough words to evaluate."
    agent._user_turn_started_at = time.monotonic()

    await asyncio.wait_for(agent._run_user_turn_checks(1), timeout=0.1)

    assert len(calls) == 2
    assert calls[1][0] == agent._latest_user_transcript
    assert calls[0][2] == 1
    assert session.llm.calls == 0


@pytest.mark.asyncio
async def test_short_pause_keeps_checkpoint_task_until_agent_speaks() -> None:
    agent, _ = _running_agent("CONTINUE")
    agent._user_speaking = False

    agent._on_user_state_changed(
        UserStateChangedEvent(old_state="listening", new_state="speaking")
    )
    task = agent._user_turn_task

    assert task is not None
    assert agent._user_speaking is True

    agent._on_user_state_changed(
        UserStateChangedEvent(old_state="speaking", new_state="listening")
    )
    await asyncio.sleep(0)

    assert agent._user_speaking is True
    assert not task.cancelled()

    generation = agent._user_turn_generation
    agent._on_user_state_changed(
        UserStateChangedEvent(old_state="listening", new_state="speaking")
    )
    assert agent._user_turn_generation == generation
    assert agent._user_turn_task is task

    agent._on_agent_state_changed(
        AgentStateChangedEvent(old_state="thinking", new_state="speaking")
    )
    await asyncio.sleep(0)

    assert agent._user_speaking is False
    assert task.cancelled()


def test_interim_transcript_keeps_most_complete_snapshot() -> None:
    agent, _ = _running_agent("CONTINUE")

    agent._on_user_input_transcribed(
        UserInputTranscribedEvent(
            transcript="I built a production monitoring dashboard", is_final=False
        )
    )
    agent._on_user_input_transcribed(
        UserInputTranscribedEvent(
            transcript="I built a production monitoring dashboard", is_final=True
        )
    )
    agent._on_user_input_transcribed(
        UserInputTranscribedEvent(
            transcript="Then deployed it for the support team", is_final=False
        )
    )
    agent._on_user_input_transcribed(
        UserInputTranscribedEvent(
            transcript="Then deployed it for the support team", is_final=True
        )
    )

    assert agent._latest_user_transcript == (
        "I built a production monitoring dashboard "
        "Then deployed it for the support team"
    )


@pytest.mark.asyncio
async def test_away_state_prompts_once_until_user_speaks() -> None:
    agent, session = _running_agent("CONTINUE")
    agent._user_speaking = False

    away = UserStateChangedEvent(old_state="listening", new_state="away")
    agent._on_user_state_changed(away)
    agent._on_user_state_changed(away)

    assert session.said == [
        ("Take your time. Let me know when you're ready.", {"allow_interruptions": True})
    ]

    agent._on_user_state_changed(
        UserStateChangedEvent(old_state="away", new_state="speaking")
    )

    assert agent._silence_prompted is False
    assert agent._user_turn_task is not None
    agent._user_turn_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await agent._user_turn_task


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

    transcript = "I solved the outage with retries and monitoring."
    decision = await agent._evaluate_user_turn(transcript, 15.0)

    assert decision is not None
    assert decision.to_interrupt is True
    assert decision.reason == "irrelevant"
    assert decision.rational == "The response shows clear evidence of irrelevant."
    evaluator_messages = session.llm.chat_contexts[0].messages()
    assert len(evaluator_messages) == 1
    prompt = evaluator_messages[0].text_content or ""
    assert "What did you build and what problem did it solve?" in prompt
    assert "2 minute(s) elapsed" in prompt
    assert transcript in prompt
    assert "Never interrupt the candidate" not in prompt
