from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Any, Literal

from livekit.agents import (
    Agent,
    AgentStateChangedEvent,
    UserInputTranscribedEvent,
    UserStateChangedEvent,
)
from livekit.agents.llm import ChatContext
from pydantic import BaseModel, Field, model_validator

from prompt import load_prompt, render_prompt

logger = logging.getLogger(__name__)

SESSION_TIMER_INTERVAL_SECONDS = 60
SESSION_TIMER_ROLE = "developer"
USER_TURN_EVALUATION_INTERVAL_SECONDS = 15.0
USER_TURN_EVALUATION_TIMEOUT_SECONDS = 3.0
MIN_USER_TURN_EVALUATION_WORDS = 5
OVER_DETAILED_MIN_DURATION_SECONDS = 45.0
INTERRUPTION_EVALUATION_PROMPT_URL = "prompts/interruption/v1.md"
INTERRUPTION_REASONS = {
    "answer_complete",
    "irrelevant",
    "over_detailed",
    "repetition",
    "time_pressure",
}


class InterruptionDecision(BaseModel):
    schema_version: Literal["1.0"]
    to_interrupt: bool
    reason: Literal[
        "answer_complete",
        "irrelevant",
        "none",
        "over_detailed",
        "repetition",
        "time_pressure",
    ]
    rational: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def validate_interruption_contract(self) -> InterruptionDecision:
        if not self.rational.strip():
            raise ValueError("rational must contain a concise evidence summary")
        if not self.to_interrupt and self.reason != "none":
            raise ValueError("continue decisions must use reason=none")
        if self.to_interrupt and self.reason == "none":
            raise ValueError("interrupt decisions must include a reason")
        return self


class UnifiedAgent(Agent):
    def __init__(
        self,
        *,
        instructions: str,
        tools: list[Any],
        initial_reply: str,
        participant_identity: str | None = None,
        room_name: str | None = None,
        manage_candidate_turns: bool = False,
    ) -> None:
        self.initial_reply = initial_reply
        self.participant_identity = participant_identity
        self.room_name = room_name
        self.manage_candidate_turns = manage_candidate_turns
        self._session_timer_task: asyncio.Task[None] | None = None
        self._user_turn_task: asyncio.Task[None] | None = None
        self._user_turn_started_at = 0.0
        self._user_turn_generation = 0
        self._user_speaking = False
        self._user_audio_speaking = False
        self._final_user_transcript = ""
        self._latest_user_transcript = ""
        self._last_evaluated_transcript = ""
        self._interrupting = False
        self._silence_prompted = False
        self._turn_listeners_registered = False
        self._interruption_evaluation_template = load_prompt(
            INTERRUPTION_EVALUATION_PROMPT_URL
        )

        super().__init__(instructions=instructions, tools=list(tools))

    def _build_elapsed_time_context(self, elapsed_minutes: int) -> str:
        return (
            f"[Internal timing context: {elapsed_minutes} minute"
            f"{'s' if elapsed_minutes != 1 else ''} elapsed since this session started. "
            "Use this only for pacing. Do not mention this timing message to the candidate "
            "unless explicitly relevant.]"
        )

    async def _inject_elapsed_time_context(self, elapsed_minutes: int) -> None:
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.add_message(
            role=SESSION_TIMER_ROLE,
            content=self._build_elapsed_time_context(elapsed_minutes),
            extra={
                "internal_timer": True,
                "elapsed_minutes": elapsed_minutes,
            },
        )
        await self.update_chat_ctx(chat_ctx)
        logger.info(
            "Injected session timing context: elapsed_minutes=%s, role=%s",
            elapsed_minutes,
            SESSION_TIMER_ROLE,
        )

    async def _run_session_timer(self) -> None:
        elapsed_minutes = 0
        while True:
            await asyncio.sleep(SESSION_TIMER_INTERVAL_SECONDS)
            elapsed_minutes += 1
            try:
                await self._inject_elapsed_time_context(elapsed_minutes)
            except Exception as e:
                logger.warning("Failed to inject session timing context: %s", e)

    def _start_session_timer(self) -> None:
        if self._session_timer_task is not None and not self._session_timer_task.done():
            return
        self._session_timer_task = asyncio.create_task(
            self._run_session_timer(),
            name=f"session-timer:{self.room_name or self.participant_identity or 'agent'}",
        )

    async def _stop_session_timer(self) -> None:
        if self._session_timer_task is None:
            return
        self._session_timer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._session_timer_task
        self._session_timer_task = None

    async def on_enter(self) -> None:
        self._start_session_timer()
        if self.manage_candidate_turns:
            self.session.on("user_state_changed", self._on_user_state_changed)
            self.session.on("user_input_transcribed", self._on_user_input_transcribed)
            self.session.on("agent_state_changed", self._on_agent_state_changed)
            self._turn_listeners_registered = True
        started_at = time.perf_counter()
        logger.info(
            "startup_phase phase=first_generate_reply_start room=%s",
            self.room_name,
        )
        try:
            await self.session.generate_reply(instructions=self.initial_reply)
        finally:
            logger.info(
                "startup_phase phase=first_generate_reply_end room=%s elapsed_ms=%.2f",
                self.room_name,
                (time.perf_counter() - started_at) * 1000,
            )

    async def on_exit(self) -> None:
        if self._turn_listeners_registered:
            self.session.off("user_state_changed", self._on_user_state_changed)
            self.session.off("user_input_transcribed", self._on_user_input_transcribed)
            self.session.off("agent_state_changed", self._on_agent_state_changed)
            self._turn_listeners_registered = False
        self._user_turn_generation += 1
        if self._user_turn_task is not None:
            self._user_turn_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._user_turn_task
            self._user_turn_task = None
        await self._stop_session_timer()
        await super().on_exit()

    def _on_user_state_changed(self, ev: UserStateChangedEvent) -> None:
        self._user_audio_speaking = ev.new_state == "speaking"
        if ev.new_state == "speaking":
            if self._interrupting:
                return
            if self._user_speaking:
                return
            self._user_speaking = True
            self._interrupting = False
            self._silence_prompted = False
            self._final_user_transcript = ""
            self._latest_user_transcript = ""
            self._last_evaluated_transcript = ""
            self._user_turn_generation += 1
            self._user_turn_started_at = time.monotonic()
            generation = self._user_turn_generation
            self._user_turn_task = asyncio.create_task(
                self._run_user_turn_checks(generation),
                name=f"user-turn-checks:{self.room_name or generation}",
            )
            logger.info("candidate_turn_started generation=%d", generation)
            return

        # LiveKit toggles listening during short pauses inside one logical turn.
        # The next agent speech is the reliable boundary for that turn.
        if ev.new_state == "away":
            self._finish_user_turn()
        if ev.new_state == "away" and not self._silence_prompted:
            self._silence_prompted = True
            logger.info("candidate_no_answer_nudge")
            self.session.say(
                "Take your time. Let me know when you're ready.",
                allow_interruptions=True,
            )

    def _on_agent_state_changed(self, ev: AgentStateChangedEvent) -> None:
        if ev.new_state == "speaking" and not self._interrupting:
            self._finish_user_turn()

    def _finish_user_turn(self) -> None:
        if not self._user_speaking:
            return
        self._user_speaking = False
        self._user_audio_speaking = False
        self._user_turn_generation += 1
        if self._user_turn_task is not None:
            self._user_turn_task.cancel()
        logger.info("candidate_turn_finished generation=%d", self._user_turn_generation)

    def _on_user_input_transcribed(self, ev: UserInputTranscribedEvent) -> None:
        if not self._user_speaking:
            return
        transcript = " ".join(ev.transcript.split())
        if not transcript:
            return
        if ev.is_final:
            self._final_user_transcript = (
                f"{self._final_user_transcript} {transcript}".strip()
            )
            self._latest_user_transcript = self._final_user_transcript
            return
        candidate = f"{self._final_user_transcript} {transcript}".strip()
        if len(candidate.split()) >= len(self._latest_user_transcript.split()):
            self._latest_user_transcript = candidate

    async def _run_user_turn_checks(self, generation: int) -> None:
        next_check = self._user_turn_started_at + USER_TURN_EVALUATION_INTERVAL_SECONDS
        try:
            while generation == self._user_turn_generation and self._user_speaking:
                await asyncio.sleep(max(0.0, next_check - time.monotonic()))
                if generation != self._user_turn_generation or not self._user_speaking:
                    return

                transcript = self._latest_user_transcript
                words = len(transcript.split())
                if words < MIN_USER_TURN_EVALUATION_WORDS:
                    logger.info(
                        "candidate_turn_checkpoint_skipped generation=%d "
                        "reason=insufficient_transcript words=%d",
                        generation,
                        words,
                    )
                elif transcript == self._last_evaluated_transcript:
                    # ponytail: retry stale interim text next tick; add audio recovery
                    # only if real sessions show persistent STT gaps.
                    logger.info(
                        "candidate_turn_checkpoint_skipped generation=%d "
                        "reason=unchanged_transcript words=%d",
                        generation,
                        words,
                    )
                else:
                    self._last_evaluated_transcript = transcript
                    duration = time.monotonic() - self._user_turn_started_at
                    if await self._evaluate_and_maybe_interrupt(
                        transcript, duration, generation
                    ):
                        return

                next_check += USER_TURN_EVALUATION_INTERVAL_SECONDS
                while next_check <= time.monotonic():
                    next_check += USER_TURN_EVALUATION_INTERVAL_SECONDS
        finally:
            if self._user_turn_task is asyncio.current_task():
                self._user_turn_task = None

    def _build_user_turn_evaluation_prompt(
        self, transcript: str, duration: float
    ) -> str:
        interviewer_message = self._latest_interviewer_message()
        elapsed_minutes = self._elapsed_session_minutes()
        session_timing = (
            f"{elapsed_minutes} minute(s) elapsed"
            if elapsed_minutes is not None
            else "unavailable"
        )
        return render_prompt(
            self._interruption_evaluation_template,
            context={
                "response_duration": f"{duration:.1f}",
                "session_timing": session_timing,
                "interviewer_message": interviewer_message,
                "candidate_response": transcript,
                "over_detailed_min_duration": (
                    f"{OVER_DETAILED_MIN_DURATION_SECONDS:.0f}"
                ),
            },
        )

    def _latest_interviewer_message(self) -> str:
        for message in reversed(self.chat_ctx.messages()):
            if message.role == "assistant" and message.text_content:
                return message.text_content
        return "No interviewer message is available."

    def _elapsed_session_minutes(self) -> int | None:
        for message in reversed(self.chat_ctx.messages()):
            if not message.extra or not message.extra.get("internal_timer"):
                continue
            elapsed_minutes = message.extra.get("elapsed_minutes")
            if isinstance(elapsed_minutes, int):
                return elapsed_minutes
        return None

    @staticmethod
    def _parse_interruption_decision(
        response: str,
    ) -> InterruptionDecision | None:
        try:
            return InterruptionDecision.model_validate_json(response)
        except Exception:
            return None

    async def _evaluate_user_turn(
        self, transcript: str, duration: float
    ) -> InterruptionDecision | None:
        # Keep the classifier independent from the main agent's long behavioral
        # prompt. That prompt contains turn-taking rules which can bias this
        # separate decision toward always continuing.
        eval_ctx = ChatContext.empty()
        eval_ctx.add_message(
            role="system",
            content=self._build_user_turn_evaluation_prompt(transcript, duration),
        )

        async def collect_response() -> str:
            stream = self.session.llm.chat(
                chat_ctx=eval_ctx,
                response_format=InterruptionDecision,
            )
            response = ""
            async for chunk in stream:
                content = chunk.delta.content if chunk.delta is not None else None
                if content:
                    response += content
            return response

        response = await asyncio.wait_for(
            collect_response(), timeout=USER_TURN_EVALUATION_TIMEOUT_SECONDS
        )

        decision = self._parse_interruption_decision(response)
        logger.info(
            "User-turn evaluator raw_decision=%r to_interrupt=%s "
            "parsed_reason=%s rational=%r",
            response.strip(),
            decision.to_interrupt if decision is not None else None,
            decision.reason if decision is not None else "invalid",
            decision.rational if decision is not None else "invalid",
        )
        if decision is None:
            logger.warning(
                "Ignoring malformed user-turn evaluation decision: %r", response
            )
        return decision

    async def _evaluate_and_maybe_interrupt(
        self, transcript: str, duration: float, generation: int
    ) -> bool:
        logger.info(
            "candidate_turn_checkpoint generation=%d words=%d duration=%.1fs",
            generation,
            len(transcript.split()),
            duration,
        )
        try:
            decision = await self._evaluate_user_turn(transcript, duration)
            if generation != self._user_turn_generation or not self._user_speaking:
                return False
            if decision is None or not decision.to_interrupt:
                logger.info("User-turn evaluation decided to continue listening")
                return False

            if (
                decision.reason == "over_detailed"
                and duration < OVER_DETAILED_MIN_DURATION_SECONDS
            ):
                logger.info(
                    "Ignoring early over_detailed decision duration=%.1fs minimum=%.1fs",
                    duration,
                    OVER_DETAILED_MIN_DURATION_SECONDS,
                )
                return False

            action_by_reason = {
                "answer_complete": "Move directly to the next interview question.",
                "irrelevant": "Briefly rephrase the unanswered question.",
                "over_detailed": "Move directly to the next interview question.",
                "repetition": "Ask one focused follow-up based on the useful content.",
                "time_pressure": "Move directly to the next interview question.",
            }
            logger.info(
                "Interrupting candidate: reason=%s rational=%r",
                decision.reason,
                decision.rational,
            )
            self._interrupting = True
            await self.session.commit_user_turn(
                transcript_timeout=1.5,
                stt_flush_duration=0.2,
                skip_reply=True,
            )
            await self.session.generate_reply(
                instructions=(
                    "Interrupt the candidate now because the interview turn was "
                    f"classified as {decision.reason}. Begin with a brief, natural, "
                    "scenario-relevant variation of 'Let me pause you there.' "
                    "Acknowledge useful content when appropriate. Then: "
                    f"{action_by_reason[decision.reason]} "
                    f"Evaluator evidence: {decision.rational} "
                    "Keep the interruption concise."
                ),
                allow_interruptions=False,
            )
            self._user_speaking = False
            self._user_audio_speaking = False
            self._user_turn_generation += 1
            return True
        except asyncio.TimeoutError:
            logger.warning("User-turn evaluation timed out after %.1fs", duration)
        except Exception as e:
            logger.warning("Failed to evaluate or interrupt user turn: %s", e)
        finally:
            self._interrupting = False
            if (
                getattr(self.session, "user_state", None) == "speaking"
                and not self._user_speaking
            ):
                self._on_user_state_changed(
                    UserStateChangedEvent(old_state="listening", new_state="speaking")
                )
        return False
