from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from enum import Enum

from livekit import rtc
from livekit.agents import AgentSession, ChatContext, function_tool, llm
from livekit.plugins import openai
from pydantic import BaseModel, Field

from session import DEFAULT_OPENROUTER_MODEL

logger = logging.getLogger(__name__)

SCREEN_FEEDBACK_INTERVAL_SECONDS = 10
SCREEN_FEEDBACK_COOLDOWN_SECONDS = 30
SCREEN_FEEDBACK_STALL_SECONDS = 60
SCREEN_FEEDBACK_CONFIDENCE_THRESHOLD = 0.8
SURFACE_STATE_TOPIC = "candidate.surface_state"
SUPPORTED_SURFACES = ("code", "whiteboard")
SCREEN_SHARE_REQUIRED_MESSAGE = (
    "Please share your entire screen and keep the active code editor or whiteboard "
    "visible so I can look at your work and guide you."
)

ON_DEMAND_ANALYSIS_PROMPT = (
    "Inspect the candidate's current shared code editor or whiteboard for their "
    "explicit request. In feedback, first state one concrete detail visibly present "
    "in their work, then give one small Socratic hint or next step. Do not give the "
    "full solution. Use plain spoken text under forty-five words with no code or "
    "formatting. Set should_speak to true. Confidence reflects how clearly the "
    "relevant work is visible. Keep reason short and internal."
)
DEVIATION_ANALYSIS_PROMPT = (
    "You are a silent technical interview observer checking whether the candidate's "
    "current technique is fundamentally non-viable. Set should_speak to true only "
    "when the visible approach has no credible path to a correct solution without "
    "changing strategy, or shows a clear conceptual misconception. Incomplete code, "
    "ordinary syntax mistakes, missing edge cases, inefficiency, and viable "
    "alternative approaches do not qualify. If should_speak is true, briefly point "
    "out the strategic concern and ask them to consider a different technique. Never "
    "give the full answer. Use one or two short Socratic sentences under thirty words "
    "with plain spoken text and no code or formatting. Do not repeat the last visual "
    "nudge."
)
STALL_ANALYSIS_PROMPT = (
    "You are a silent technical interview observer. The candidate has made no code "
    "or whiteboard progress for at least sixty seconds. Set should_speak to true when "
    "the relevant work is visible and give one small Socratic hint or technique that "
    "can restart progress. Never give the full answer. Feedback must be one or two "
    "short sentences under thirty words, with plain spoken text and no code or "
    "formatting. Do not repeat the last visual nudge."
)


class ScreenFeedbackDecision(BaseModel):
    should_speak: bool
    confidence: float = Field(ge=0, le=1)
    feedback: str
    reason: str


class ScreenFeedbackTrigger(Enum):
    DEVIATION = "deviation"
    STALL = "stall"


@dataclass(frozen=True)
class ScreenSnapshot:
    frame: rtc.VideoFrame
    question: dict[str, str]
    revision: int
    inactive_seconds: float


class ScreenFeedbackRuntime:
    """Samples a shared screen and speaks only high-confidence interview nudges."""

    def __init__(
        self,
        *,
        room: rtc.Room,
        participant_identity: str,
    ) -> None:
        self._room = room
        self._participant_identity = participant_identity
        self._llm = openai.LLM.with_openrouter(model=DEFAULT_OPENROUTER_MODEL)
        self._session: AgentSession | None = None
        self._active_question: dict[str, str] | None = None
        self._surface_visible = False
        self._visible_surface: str | None = None
        self._latest_frame: rtc.VideoFrame | None = None
        self._video_stream: rtc.VideoStream | None = None
        self._video_task: asyncio.Task[None] | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._content_revision = 0
        self._last_evaluated_revision: int | None = 0
        self._stall_evaluated_revision: int | None = None
        self._unchanged_since = time.monotonic()
        self._last_feedback = ""
        self._last_spoken_at = 0.0
        self._started = False

    async def start(self, session: AgentSession) -> None:
        if self._started:
            return
        self._started = True
        self._session = session

        self._room.on("track_published", self._on_track_published)
        self._room.on("track_subscribed", self._on_track_subscribed)
        self._room.on("track_unsubscribed", self._on_track_unsubscribed)
        self._room.on("data_received", self._on_data_received)

        self._sync_screen_subscription()

        self._timer_task = asyncio.create_task(
            self._run_timer(),
            name=f"screen-feedback:{self._room.name}",
        )
        logger.info(
            "Screen feedback timer started room=%s interval_seconds=%d",
            self._room.name,
            SCREEN_FEEDBACK_INTERVAL_SECONDS,
        )

    async def close(self) -> None:
        if not self._started:
            return
        self._started = False

        self._room.off("track_published", self._on_track_published)
        self._room.off("track_subscribed", self._on_track_subscribed)
        self._room.off("track_unsubscribed", self._on_track_unsubscribed)
        self._room.off("data_received", self._on_data_received)

        if self._timer_task is not None:
            self._timer_task.cancel()
            await asyncio.gather(self._timer_task, return_exceptions=True)
            self._timer_task = None
        await self._stop_video_stream()
        if self._cleanup_tasks:
            await asyncio.gather(*self._cleanup_tasks, return_exceptions=True)
            self._cleanup_tasks.clear()

    async def on_question_started(self, question: dict[str, str]) -> None:
        surface = question.get("surface")
        self._active_question = question if surface in SUPPORTED_SURFACES else None
        self._content_revision = 0
        self._last_evaluated_revision = 0
        self._stall_evaluated_revision = None
        self._unchanged_since = time.monotonic()
        self._last_feedback = ""
        self._last_spoken_at = 0.0
        self._sync_screen_subscription()
        logger.info(
            "Screen feedback question state room=%s question_id=%s surface=%s active=%s",
            self._room.name,
            question.get("id"),
            surface,
            self._active_question is not None,
        )

    def _on_track_published(
        self,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if (
            participant.identity == self._participant_identity
            and publication.source == rtc.TrackSource.SOURCE_SCREENSHARE
        ):
            self._sync_screen_subscription()

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if (
            participant.identity == self._participant_identity
            and publication.source == rtc.TrackSource.SOURCE_SCREENSHARE
        ):
            self._start_video_stream(track)

    def _on_track_unsubscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if (
            participant.identity == self._participant_identity
            and publication.source == rtc.TrackSource.SOURCE_SCREENSHARE
        ):
            self._latest_frame = None
            if self._video_task is not None:
                self._video_task.cancel()
                self._video_task = None
            if self._video_stream is not None:
                stream = self._video_stream
                self._video_stream = None
                task = asyncio.create_task(stream.aclose())
                self._cleanup_tasks.add(task)
                task.add_done_callback(self._cleanup_tasks.discard)

    def _on_data_received(self, packet: rtc.DataPacket) -> None:
        if packet.topic != SURFACE_STATE_TOPIC:
            return
        if (
            packet.participant is None
            or packet.participant.identity != self._participant_identity
        ):
            return
        try:
            payload = json.loads(packet.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            logger.warning("Ignored invalid candidate surface state payload")
            return
        if (
            not isinstance(payload, dict)
            or payload.get("type") != "candidate_surface_state"
        ):
            return

        surface = payload.get("surface")
        previous_surface = self._visible_surface
        was_visible = self._surface_visible
        self._surface_visible = payload.get("visible") is True
        self._visible_surface = surface if surface in SUPPORTED_SURFACES else None
        content_revision = payload.get("content_revision")
        if (
            not isinstance(content_revision, int)
            or isinstance(content_revision, bool)
            or content_revision < 0
        ):
            content_revision = 0

        now = time.monotonic()
        if (
            not self._surface_visible
            or not was_visible
            or previous_surface != self._visible_surface
        ):
            self._content_revision = content_revision
            self._last_evaluated_revision = content_revision
            self._stall_evaluated_revision = None
            self._unchanged_since = now
        elif content_revision != self._content_revision:
            self._content_revision = content_revision
            self._stall_evaluated_revision = None
            self._unchanged_since = now
        self._sync_screen_subscription()

    def _screen_publication(self) -> rtc.RemoteTrackPublication | None:
        participant = self._room.remote_participants.get(self._participant_identity)
        if participant is None:
            return None
        for publication in participant.track_publications.values():
            if publication.source == rtc.TrackSource.SOURCE_SCREENSHARE:
                return publication
        return None

    def _should_subscribe_to_screen(self) -> bool:
        question = self._active_question
        return bool(
            question
            and self._surface_visible
            and self._visible_surface == question.get("surface")
        )

    def _sync_screen_subscription(self) -> None:
        if not self._started:
            return
        publication = self._screen_publication()
        if publication is None:
            return
        should_subscribe = self._should_subscribe_to_screen()
        if publication.subscribed != should_subscribe:
            publication.set_subscribed(should_subscribe)
        if (
            should_subscribe
            and publication.track is not None
            and self._video_stream is None
        ):
            self._start_video_stream(publication.track)

    def _start_video_stream(self, track: rtc.Track) -> None:
        if self._video_task is not None:
            self._video_task.cancel()
        if self._video_stream is not None:
            task = asyncio.create_task(self._video_stream.aclose())
            self._cleanup_tasks.add(task)
            task.add_done_callback(self._cleanup_tasks.discard)

        self._video_stream = rtc.VideoStream(track, capacity=1)
        stream = self._video_stream

        async def consume() -> None:
            try:
                async for event in stream:
                    self._latest_frame = event.frame
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Screen-share video stream failed room=%s", self._room.name
                )

        self._video_task = asyncio.create_task(
            consume(),
            name=f"screen-feedback-video:{self._room.name}",
        )

    async def _stop_video_stream(self) -> None:
        if self._video_task is not None:
            self._video_task.cancel()
            await asyncio.gather(self._video_task, return_exceptions=True)
            self._video_task = None
        if self._video_stream is not None:
            await self._video_stream.aclose()
            self._video_stream = None
        self._latest_frame = None

    async def _run_timer(self) -> None:
        while True:
            await asyncio.sleep(SCREEN_FEEDBACK_INTERVAL_SECONDS)
            try:
                await self._evaluate_latest_frame()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Screen feedback evaluation failed room=%s", self._room.name
                )

    def _can_evaluate(self) -> bool:
        session = self._session
        return bool(
            self._current_snapshot() is not None
            and session is not None
            and session.agent_state == "listening"
            and session.user_state != "speaking"
        )

    def _current_snapshot(self) -> ScreenSnapshot | None:
        question = self._active_question
        frame = self._latest_frame
        if (
            question is None
            or not self._surface_visible
            or self._visible_surface != question.get("surface")
            or frame is None
        ):
            return None
        return ScreenSnapshot(
            frame=frame,
            question=question,
            revision=self._content_revision,
            inactive_seconds=max(0.0, time.monotonic() - self._unchanged_since),
        )

    def _snapshot_is_current(self, snapshot: ScreenSnapshot) -> bool:
        question = self._active_question
        return bool(
            question is not None
            and self._surface_visible
            and question.get("id") == snapshot.question.get("id")
            and self._visible_surface == snapshot.question.get("surface")
            and self._content_revision == snapshot.revision
        )

    @staticmethod
    def _automatic_trigger(
        snapshot: ScreenSnapshot,
        *,
        last_evaluated_revision: int | None,
        stall_evaluated_revision: int | None,
    ) -> ScreenFeedbackTrigger | None:
        if (
            snapshot.inactive_seconds >= SCREEN_FEEDBACK_STALL_SECONDS
            and snapshot.revision != stall_evaluated_revision
        ):
            return ScreenFeedbackTrigger.STALL
        if snapshot.revision != last_evaluated_revision:
            return ScreenFeedbackTrigger.DEVIATION
        return None

    @staticmethod
    def _automatic_prompt(trigger: ScreenFeedbackTrigger) -> str:
        if trigger is ScreenFeedbackTrigger.STALL:
            return STALL_ANALYSIS_PROMPT
        return DEVIATION_ANALYSIS_PROMPT

    @staticmethod
    def _snapshot_prompt(snapshot: ScreenSnapshot, request_context: str) -> str:
        return (
            f"Question: {snapshot.question.get('text', '')}\n"
            f"Surface: {snapshot.question.get('surface', '')}\n"
            f"Approximate seconds without code or diagram progress: "
            f"{snapshot.inactive_seconds:.0f}\n"
            f"Analysis context: {request_context}\n"
            "Assess this current screen snapshot."
        )

    async def _analyze_frame(
        self,
        *,
        frame: rtc.VideoFrame,
        system_prompt: str,
        user_prompt: str,
    ) -> ScreenFeedbackDecision:
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="system", content=system_prompt)
        chat_ctx.add_message(
            role="user",
            content=[
                user_prompt,
                llm.ImageContent(
                    image=frame,
                    inference_width=1280,
                    inference_height=720,
                    inference_detail="high",
                ),
            ],
        )
        response = await self._llm.chat(
            chat_ctx=chat_ctx,
            response_format=ScreenFeedbackDecision,
        ).collect()
        return ScreenFeedbackDecision.model_validate_json(response.text)

    async def _analyze_snapshot(
        self,
        snapshot: ScreenSnapshot,
        *,
        system_prompt: str,
        request_context: str,
    ) -> ScreenFeedbackDecision:
        return await self._analyze_frame(
            frame=snapshot.frame,
            system_prompt=system_prompt,
            user_prompt=self._snapshot_prompt(snapshot, request_context),
        )

    @staticmethod
    def _recovery_result(
        *,
        status: str,
        candidate_message: str,
    ) -> dict[str, object]:
        return {
            "status": status,
            "screen_available": False,
            "candidate_message": candidate_message,
            "response_guidance": (
                "Say candidate_message to the candidate and continue the interview. "
                "This is a normal recoverable state. Do not call end_call."
            ),
        }

    async def inspect_shared_screen(self, user_request: str) -> dict[str, object]:
        """Inspect the latest shared-screen frame for an explicit candidate request."""
        question = self._active_question
        if (
            question is None
            or not self._surface_visible
            or self._visible_surface != question.get("surface")
        ):
            return self._recovery_result(
                status="surface_unavailable",
                candidate_message=SCREEN_SHARE_REQUIRED_MESSAGE,
            )
        if self._screen_publication() is None:
            return self._recovery_result(
                status="screen_share_required",
                candidate_message=SCREEN_SHARE_REQUIRED_MESSAGE,
            )
        snapshot = self._current_snapshot()
        if snapshot is None:
            return self._recovery_result(
                status="loading",
                candidate_message=(
                    "Please keep screen sharing enabled with the active editor or "
                    "whiteboard visible for a moment, then ask me again."
                ),
            )

        request = user_request.strip() if isinstance(user_request, str) else ""
        try:
            decision = await self._analyze_snapshot(
                snapshot,
                system_prompt=ON_DEMAND_ANALYSIS_PROMPT,
                request_context=(
                    "Candidate request: "
                    f"{request or 'Give feedback on my current work.'}"
                ),
            )
        except Exception:
            logger.exception(
                "On-demand screen inspection failed room=%s", self._room.name
            )
            return {
                "status": "error",
                "response_guidance": (
                    "Ask the candidate to keep working and repeat their screen-related "
                    "question in a moment."
                ),
            }

        if not self._snapshot_is_current(snapshot):
            return {
                "status": "changed",
                "response_guidance": (
                    "The candidate changed their work during inspection. Call "
                    "inspect_shared_screen once more before answering."
                ),
            }

        feedback = decision.feedback.strip()
        if not feedback:
            feedback = (
                "Ask the candidate which specific part of the current approach they "
                "want to reason through."
            )

        now = time.monotonic()
        self._last_evaluated_revision = snapshot.revision
        self._stall_evaluated_revision = (
            snapshot.revision
            if snapshot.inactive_seconds >= SCREEN_FEEDBACK_STALL_SECONDS
            else None
        )
        self._last_feedback = feedback
        self._last_spoken_at = now
        logger.info(
            "On-demand screen inspection completed room=%s question_id=%s revision=%d",
            self._room.name,
            question.get("id"),
            snapshot.revision,
        )
        return {
            "status": "ok",
            "surface": question.get("surface"),
            "observation_and_hint": feedback,
        }

    async def _evaluate_latest_frame(self) -> None:
        if not self._can_evaluate():
            return

        snapshot = self._current_snapshot()
        session = self._session
        if snapshot is None or session is None:
            return

        trigger = self._automatic_trigger(
            snapshot,
            last_evaluated_revision=self._last_evaluated_revision,
            stall_evaluated_revision=self._stall_evaluated_revision,
        )
        if trigger is None:
            logger.debug(
                "Skipped unchanged screen room=%s revision=%d inactive_seconds=%.0f",
                self._room.name,
                snapshot.revision,
                snapshot.inactive_seconds,
            )
            return

        decision = await self._analyze_snapshot(
            snapshot,
            system_prompt=self._automatic_prompt(trigger),
            request_context=(
                f"Last spoken visual nudge: {self._last_feedback or 'none'}"
            ),
        )

        if not self._snapshot_is_current(snapshot):
            return
        self._last_evaluated_revision = snapshot.revision
        if trigger is ScreenFeedbackTrigger.STALL:
            self._stall_evaluated_revision = snapshot.revision

        feedback = decision.feedback.strip()
        decision_time = time.monotonic()
        should_speak = (
            decision.should_speak
            and decision.confidence >= SCREEN_FEEDBACK_CONFIDENCE_THRESHOLD
            and bool(feedback)
            and decision_time - self._last_spoken_at >= SCREEN_FEEDBACK_COOLDOWN_SECONDS
            and self._can_evaluate()
        )
        logger.info(
            "Screen feedback decision room=%s question_id=%s trigger=%s "
            "should_speak=%s confidence=%.2f",
            self._room.name,
            snapshot.question.get("id"),
            trigger.value,
            should_speak,
            decision.confidence,
        )
        if not should_speak:
            return

        session.say(feedback, allow_interruptions=True, add_to_chat_ctx=True)
        self._last_feedback = feedback
        self._last_spoken_at = decision_time


def build_screen_inspection_tool(runtime: ScreenFeedbackRuntime):
    @function_tool(
        name="inspect_shared_screen",
        description=(
            "Inspect the candidate's current shared code editor or whiteboard. You MUST "
            "call this before answering when the candidate asks for a hint, expresses a "
            "doubt, asks whether their current work is correct, asks what is visible, or "
            "asks what to do next. Use only during code or whiteboard questions. Pass "
            "their request in user_request. The tool returns a brief visible observation "
            "and one next-step hint without adding the image to the main chat context. "
            "If screen sharing is disabled, say the returned candidate_message and "
            "continue the interview; never end the call for this reason."
        ),
    )
    async def inspect_shared_screen(user_request: str = "") -> dict[str, object]:
        return await runtime.inspect_shared_screen(user_request)

    return inspect_shared_screen
