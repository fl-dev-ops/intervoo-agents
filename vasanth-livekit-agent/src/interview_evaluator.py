from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any

from livekit import rtc
from livekit.agents import (
    Agent,
    ChatContext,
    RunContext,
    function_tool,
    llm,
)
from livekit.plugins import openai
from pydantic import BaseModel, Field

from session import DEFAULT_OPENROUTER_MODEL

logger = logging.getLogger(__name__)

CODE_ANSWER_TOPIC = "candidate.code_answer"
EVALUATOR_HANDOFF_MESSAGE = "Please wait while I prepare my feedback."
MAX_CODE_ANSWER_CHARS = 20_000
EVALUATION_TIMEOUT_SECONDS = 30
CODE_ANSWER_DRAIN_TIMEOUT_SECONDS = 1
SUPPORTED_CODE_LANGUAGES = {"java", "javascript", "python"}


class AssessmentResult(str, Enum):
    CORRECT = "correct"
    PARTIAL = "partial"
    INCORRECT = "incorrect"
    NOT_ATTEMPTED = "not_attempted"


class CodeResult(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    SUBSTANTIALLY_CORRECT = "substantially_correct"
    PARTIAL = "partial"
    INCORRECT = "incorrect"
    NOT_ATTEMPTED = "not_attempted"


class ClosureRoute(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    MIXED = "mixed"
    FEEDBACK_ONLY = "feedback_only"


class QuestionAssessment(BaseModel):
    question_id: str
    result: AssessmentResult
    is_fundamental: bool
    code_result: CodeResult
    evidence: str = Field(min_length=1)


class InterviewEvaluation(BaseModel):
    confidence: float = Field(ge=0, le=1)
    assessments: list[QuestionAssessment]
    strengths: list[str] = Field(
        min_length=1,
        max_length=3,
        description=(
            "Complete, natural sentences spoken directly to the candidate using "
            "'you' or 'your', each tied to specific interview evidence."
        ),
    )
    gaps: list[str] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Constructive, complete sentences spoken directly to the candidate "
            "using 'you' or 'your', with a specific missing detail or next step."
        ),
    )
    improvement_direction: str = Field(
        min_length=1,
        description=(
            "One actionable, complete suggestion addressed directly to the "
            "candidate using 'you' or 'your'."
        ),
    )
    experience_calibration: str | None = Field(
        default=None,
        description=(
            "When needed, one complete second-person sentence explaining the "
            "depth expected for the candidate's stated experience."
        ),
    )


@dataclass(frozen=True)
class ClosureDecision:
    route: ClosureRoute
    rating: float | None = None


def _normalize_question(record: object) -> dict[str, Any] | None:
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
    if surface not in {"verbal", "code", "whiteboard"}:
        surface = "verbal"

    answer_mode_raw = record.get("answerMode")
    answer_mode = (
        answer_mode_raw.strip().lower()
        if isinstance(answer_mode_raw, str)
        else ("verbal" if surface == "verbal" else "surface")
    )
    if answer_mode not in {"verbal", "surface"}:
        answer_mode = "verbal" if surface == "verbal" else "surface"

    language_raw = record.get("language")
    language = (
        language_raw.strip().lower()
        if isinstance(language_raw, str)
        and language_raw.strip().lower() in SUPPORTED_CODE_LANGUAGES
        else "javascript"
    )
    return {
        "id": question_id.strip(),
        "text": text.strip(),
        "surface": surface,
        "answerMode": answer_mode,
        "language": language,
    }


def _message_turn(item: object) -> dict[str, str] | None:
    if not isinstance(item, llm.ChatMessage):
        return None
    if item.extra.get("internal_timer") is True:
        return None
    if item.role not in {"assistant", "user"}:
        return None
    text = item.text_content
    if not isinstance(text, str) or not text.strip():
        return None
    return {"role": item.role, "text": text.strip()}


class InterviewEvidenceTracker:
    """Collects planned question turns and exact code without polluting chat history."""

    def __init__(
        self,
        *,
        questions: object,
        participant_identity: str,
    ) -> None:
        raw_questions = questions if isinstance(questions, list) else []
        self._questions = [
            normalized
            for raw in raw_questions
            if (normalized := _normalize_question(raw)) is not None
        ]
        self._questions_by_id = {
            question["id"]: question for question in self._questions
        }
        self._participant_identity = participant_identity
        self._active_question_id: str | None = None
        self._started_question_ids: set[str] = set()
        self._turns: dict[str, list[dict[str, str]]] = {
            question["id"]: [] for question in self._questions
        }
        self._code_answers: dict[str, dict[str, Any]] = {}
        self._stream_tasks: set[asyncio.Task[None]] = set()
        self._room: rtc.Room | None = None

    def start(self, room: rtc.Room) -> None:
        room.register_text_stream_handler(CODE_ANSWER_TOPIC, self._on_code_stream)
        self._room = room

    async def close(self) -> None:
        room = self._room
        self._room = None
        if room is not None:
            try:
                room.unregister_text_stream_handler(CODE_ANSWER_TOPIC)
            except ValueError:
                pass
        if self._stream_tasks:
            await asyncio.gather(*self._stream_tasks, return_exceptions=True)
            self._stream_tasks.clear()

    async def wait_for_pending_code_answers(self) -> None:
        """Give recently delivered editor streams a bounded chance to finish."""
        await asyncio.sleep(0.25)  # let a just-submitted stream arrive
        if self._stream_tasks:
            await asyncio.wait(
                set(self._stream_tasks),
                timeout=CODE_ANSWER_DRAIN_TIMEOUT_SECONDS,
            )

    def on_question_started(self, question: dict[str, Any]) -> None:
        question_id = question.get("id")
        if not isinstance(question_id, str) or question_id not in self._questions_by_id:
            return
        self._active_question_id = question_id
        self._started_question_ids.add(question_id)

    def on_conversation_item(self, item: object) -> None:
        question_id = self._active_question_id
        if question_id is None:
            return
        turn = _message_turn(item)
        if turn is not None:
            self._turns[question_id].append(turn)

    def has_started_final_question(self) -> bool:
        if not self._questions:
            return False
        return self._questions[-1]["id"] in self._started_question_ids

    def store_code_answer(
        self,
        payload: object,
        *,
        participant_identity: str,
    ) -> bool:
        if participant_identity != self._participant_identity:
            return False
        if not isinstance(payload, dict):
            return False
        if payload.get("surface") != "code" or payload.get("answerMode") != "surface":
            return False

        question_id = payload.get("questionId")
        question = (
            self._questions_by_id.get(question_id)
            if isinstance(question_id, str)
            else None
        )
        if (
            question is None
            or question["surface"] != "code"
            or question["answerMode"] != "surface"
        ):
            return False

        language = payload.get("language")
        code = payload.get("code")
        revision = payload.get("revision")
        submitted = payload.get("submitted")
        if not isinstance(language, str) or language not in SUPPORTED_CODE_LANGUAGES:
            return False
        if not isinstance(code, str) or len(code) > MAX_CODE_ANSWER_CHARS:
            return False
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            return False
        if not isinstance(submitted, bool):
            return False

        previous = self._code_answers.get(question_id)
        if previous is not None:
            previous_revision = previous["revision"]
            if revision < previous_revision:
                return False
            if revision == previous_revision and (
                previous["submitted"] or not submitted
            ):
                return False

        self._code_answers[question_id] = {
            "language": language,
            "code": code,
            "revision": revision,
            "submitted": submitted,
        }
        return True

    def build_evidence(self) -> list[dict[str, Any]]:
        return [
            {
                **question,
                "turns": list(self._turns[question["id"]]),
                "code_answer": self._code_answers.get(question["id"]),
            }
            for question in self._questions
        ]

    def _on_code_stream(
        self,
        reader: rtc.TextStreamReader,
        participant_identity: str,
    ) -> None:
        task = asyncio.create_task(
            self._consume_code_stream(reader, participant_identity),
            name=f"candidate-code-answer:{participant_identity}",
        )
        self._stream_tasks.add(task)
        task.add_done_callback(self._stream_tasks.discard)

    async def _consume_code_stream(
        self,
        reader: rtc.TextStreamReader,
        participant_identity: str,
    ) -> None:
        try:
            raw_payload = await reader.read_all()
            payload = json.loads(raw_payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Ignored invalid candidate code answer payload")
            return
        if not self.store_code_answer(
            payload,
            participant_identity=participant_identity,
        ):
            logger.warning(
                "Ignored candidate code answer participant=%s",
                participant_identity,
            )


def conversation_turns(chat_ctx: ChatContext) -> list[dict[str, str]]:
    turns: list[dict[str, str]] = []
    for item in chat_ctx.items:
        turn = _message_turn(item)
        if turn is not None:
            turns.append(turn)
    return turns


def _has_usable_answer(question: dict[str, Any]) -> bool:
    turns = question.get("turns")
    if isinstance(turns, list):
        for turn in turns:
            if (
                isinstance(turn, dict)
                and turn.get("role") == "user"
                and isinstance(turn.get("text"), str)
                and turn["text"].strip()
            ):
                return True
    code_answer = question.get("code_answer")
    return bool(
        isinstance(code_answer, dict)
        and isinstance(code_answer.get("code"), str)
        and code_answer["code"].strip()
    )


def decide_closure(
    evidence: list[dict[str, Any]],
    evaluation: InterviewEvaluation,
) -> ClosureDecision:
    if not evidence:
        return ClosureDecision(ClosureRoute.FEEDBACK_ONLY)

    expected_ids = [question["id"] for question in evidence]
    assessment_ids = [assessment.question_id for assessment in evaluation.assessments]
    usable_ratio = sum(_has_usable_answer(question) for question in evidence) / len(
        evidence
    )
    if (
        usable_ratio < 0.5
        or evaluation.confidence < 0.6
        or len(set(assessment_ids)) != len(assessment_ids)
        or set(assessment_ids) != set(expected_ids)
    ):
        return ClosureDecision(ClosureRoute.FEEDBACK_ONLY)

    by_id = {
        assessment.question_id: assessment for assessment in evaluation.assessments
    }
    assessments = [by_id[question_id] for question_id in expected_ids]
    correct_count = sum(
        assessment.result is AssessmentResult.CORRECT for assessment in assessments
    )
    partial_count = sum(
        assessment.result is AssessmentResult.PARTIAL for assessment in assessments
    )
    failed_count = sum(
        assessment.result
        in {AssessmentResult.INCORRECT, AssessmentResult.NOT_ATTEMPTED}
        for assessment in assessments
    )
    fundamental_misses = sum(
        assessment.is_fundamental
        and assessment.result
        in {AssessmentResult.INCORRECT, AssessmentResult.NOT_ATTEMPTED}
        for assessment in assessments
    )

    written_code_is_strong = all(
        _has_usable_answer(question)
        and by_id[question["id"]].code_result is CodeResult.SUBSTANTIALLY_CORRECT
        for question in evidence
        if question.get("surface") == "code" and question.get("answerMode") == "surface"
    )
    accept = (
        correct_count / len(assessments) >= 0.8
        and fundamental_misses == 0
        and written_code_is_strong
    )
    if accept:
        return ClosureDecision(ClosureRoute.ACCEPT)

    if failed_count / len(assessments) >= 0.5 or fundamental_misses >= 2:
        return ClosureDecision(ClosureRoute.REJECT)

    raw_rating = 5 * (correct_count + 0.5 * partial_count) / len(assessments)
    rating = math.floor(raw_rating * 2 + 0.5) / 2
    return ClosureDecision(ClosureRoute.MIXED, rating=rating)


def _as_sentence(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _rating_words(value: float) -> str:
    labels = {
        0.0: "zero",
        0.5: "zero point five",
        1.0: "one",
        1.5: "one point five",
        2.0: "two",
        2.5: "two point five",
        3.0: "three",
        3.5: "three point five",
        4.0: "four",
        4.5: "four point five",
        5.0: "five",
    }
    return labels.get(value, f"{value:g}")


def render_vasanth_closure(
    decision: ClosureDecision,
    evaluation: InterviewEvaluation,
) -> str:
    strength = _as_sentence(evaluation.strengths[0])
    gap = _as_sentence(evaluation.gaps[0]) if evaluation.gaps else ""
    improvement = _as_sentence(evaluation.improvement_direction)
    calibration = (
        _as_sentence(evaluation.experience_calibration)
        if evaluation.experience_calibration
        else ""
    )

    if decision.route is ClosureRoute.ACCEPT:
        parts = [
            "Okay. Sure. So I'll just pass on my honest feedback.",
            strength,
        ]
        if gap:
            parts.extend(
                [
                    gap,
                    "Your answer was not wrong, but you can add more depth there.",
                ]
            )
        parts.extend(
            [
                "Overall, it was a clean interview.",
                "If I were interviewing you, I would definitely select you.",
                "You should be able to clear the interview with the preparation you have shown.",
                "Nice talking to you. Have a good day. All the best.",
            ]
        )
        return " ".join(part for part in parts if part)

    if decision.route is ClosureRoute.REJECT:
        return " ".join(
            part
            for part in [
                "Okay. I'll just pass on my honest feedback.",
                strength,
                gap,
                "I was expecting more.",
                calibration,
                improvement,
                "That's my honest feedback. Nice talking to you. All the best.",
            ]
            if part
        )

    if decision.route is ClosureRoute.MIXED:
        rating = _rating_words(decision.rating or 0)
        return " ".join(
            part
            for part in [
                "Okay, I'll give my honest feedback now.",
                strength,
                gap,
                f"Out of five, I would rate this interview around {rating}.",
                "For me to select you, the minimum I would expect is around three to three point five.",
                improvement,
                "Nice talking to you. Have a good day.",
            ]
            if part
        )

    return " ".join(
        part
        for part in [
            "Okay, considering the time, I'll give my honest feedback.",
            strength,
            gap,
            improvement,
            "I don't want to force a verdict from an incomplete session.",
            "Nice talking to you. Have a good day.",
        ]
        if part
    )


def render_evaluation_failure() -> str:
    return (
        "Okay, considering the time, I'll give my honest feedback. "
        "I don't have enough reliable evidence to give you a fair verdict today, "
        "so I don't want to force one. Nice talking to you. Have a good day."
    )


EndSessionCallback = Callable[[], Awaitable[None]]


class EvaluatorAgent(Agent):
    def __init__(
        self,
        *,
        chat_ctx: ChatContext,
        evaluator_prompt: str,
        evaluation_payload: dict[str, Any],
        end_session: EndSessionCallback,
    ) -> None:
        super().__init__(
            instructions=(
                "Evaluate the completed mock interview and deliver Vasanth's final "
                "feedback directly to the candidate. Keep it natural, specific, "
                "constructive, and in second-person language using 'you' and 'your'. "
                "Never read internal labels or fragment notes aloud. End the session "
                "after the feedback."
            ),
            chat_ctx=chat_ctx,
            tools=[],
        )
        self._evaluator_prompt = evaluator_prompt
        self._evaluation_payload = evaluation_payload
        self._end_session = end_session
        self._evaluator_llm = openai.LLM.with_openrouter(model=DEFAULT_OPENROUTER_MODEL)

    async def _evaluate(self) -> InterviewEvaluation:
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="system", content=self._evaluator_prompt)
        chat_ctx.add_message(
            role="user",
            content=json.dumps(self._evaluation_payload, ensure_ascii=True),
        )
        response = await self._evaluator_llm.chat(
            chat_ctx=chat_ctx,
            response_format=InterviewEvaluation,
        ).collect()
        return InterviewEvaluation.model_validate_json(response.text)

    async def on_enter(self) -> None:
        try:
            evaluation = await asyncio.wait_for(
                self._evaluate(),
                timeout=EVALUATION_TIMEOUT_SECONDS,
            )
            decision = decide_closure(
                self._evaluation_payload["planned_questions"],
                evaluation,
            )
            closure = render_vasanth_closure(decision, evaluation)
        except Exception:
            logger.exception("Interview evaluation failed")
            closure = render_evaluation_failure()

        try:
            speech_handle = self.session.say(
                closure,
                allow_interruptions=False,
                add_to_chat_ctx=True,
            )
            await speech_handle
        finally:
            await self._end_session()


def build_finish_interview_tool(
    *,
    tracker: InterviewEvidenceTracker,
    evaluator_prompt: str,
    candidate_context: dict[str, Any],
    end_session: EndSessionCallback,
):
    @function_tool(
        name="finish_interview",
        description=(
            "Required terminal handoff after the candidate completes the final "
            "planned question and any useful probe or walkthrough. Continue normal "
            "clarification and guidance while the final answer is still active; then "
            "call this immediately as your next and only action. Do not first announce "
            "that the interview is done, summarize, score, thank the candidate, or "
            "wait for another candidate message. The tool says 'Please wait while I "
            "prepare my feedback.' and then hands the completed interview to Vasanth's "
            "evaluator for final feedback. Set session_inconclusive to true only when "
            "time expired or the candidate could not continue before the final "
            "planned question."
        ),
    )
    async def finish_interview(
        context: RunContext,
        session_inconclusive: bool = False,
    ) -> Agent | dict[str, str]:
        if not session_inconclusive and not tracker.has_started_final_question():
            return {
                "status": "not_ready",
                "message": (
                    "The final planned question has not started. Continue the "
                    "interview before requesting evaluation."
                ),
            }

        current_agent = context.session.current_agent
        if current_agent is None:
            return {
                "status": "unavailable",
                "message": "The interviewer context is unavailable.",
            }
        await tracker.wait_for_pending_code_answers()
        evaluator_chat_ctx = current_agent.chat_ctx.copy(
            exclude_instructions=True,
            exclude_function_call=True,
            exclude_empty_message=True,
            exclude_handoff=True,
            exclude_config_update=True,
        )
        evaluation_payload = {
            "candidate_context": candidate_context,
            "conversation": conversation_turns(evaluator_chat_ctx),
            "planned_questions": tracker.build_evidence(),
        }
        await context.session.say(
            EVALUATOR_HANDOFF_MESSAGE,
            allow_interruptions=False,
            add_to_chat_ctx=True,
        )
        return EvaluatorAgent(
            chat_ctx=evaluator_chat_ctx,
            evaluator_prompt=evaluator_prompt,
            evaluation_payload=evaluation_payload,
            end_session=end_session,
        )

    return finish_interview
