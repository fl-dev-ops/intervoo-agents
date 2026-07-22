from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

from livekit import rtc
from livekit.agents import AgentSession, ChatContext, function_tool, llm
from livekit.plugins import openai
from pydantic import BaseModel, Field, field_validator, model_validator

from session import DEFAULT_OPENROUTER_MODEL

logger = logging.getLogger(__name__)

SCREEN_FEEDBACK_INTERVAL_SECONDS = 10
SCREEN_FEEDBACK_COOLDOWN_SECONDS = 30
SCREEN_FEEDBACK_STALL_SECONDS = 60
SCREEN_FEEDBACK_CONFIDENCE_THRESHOLD = 0.8
RESUME_FRAME_WAIT_SECONDS = 10
# Upper bound on inspected viewports before the resume path finalizes with
# whatever has been accumulated. Prevents an endless scroll loop when the vision
# model never returns a confident apparent_end for the document's true bottom.
RESUME_MAX_VIEWPORTS = 6
SURFACE_STATE_TOPIC = "candidate.surface_state"
SUPPORTED_SURFACES = ("code", "whiteboard")
SCREEN_SHARE_REQUIRED_MESSAGE = (
    "Please share your entire screen and keep the active code editor or whiteboard "
    "visible so I can look at your work and guide you."
)
RESUME_SCREEN_SHARE_REQUIRED_MESSAGE = (
    "Please share your screen, open your resume, and keep the resume clearly visible. "
    "Tell me when it is ready."
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
RESUME_VIEWPORT_ANALYSIS_PROMPT = (
    "You inspect one visible viewport of a candidate's resume. Extract only "
    "professional information: education, work experience, company names, role "
    "titles, skills, projects, and certifications. Never extract or repeat the "
    "candidate's name, email, phone number, address, photograph, links, identifiers, "
    "or other personal contact information. The accumulated resume state is supplied "
    "as text. Return facts visible in the current viewport even when they overlap "
    "with accumulated facts; the application deduplicates them. Do not infer facts "
    "that are not visible. Set end_state to more_content when text is clipped at the "
    "bottom, a visible page counter is not on its final page, or a scrollbar is "
    "clearly above the bottom. Set apparent_end only when there is positive visual "
    "evidence that the document ends here, such as the final PDF page together with "
    "an unclipped bottom, or a scrollbar at the bottom with an unclipped resume "
    "footer or bottom margin. Otherwise set uncertain. Keep end_reason short and "
    "grounded in visible evidence. When more_content is selected, provide one precise "
    "plain-spoken scroll_instruction telling the candidate what remains clipped or "
    "which next page to show."
)
RESUME_NORMALIZATION_PROMPT = (
    "Normalize accumulated professional resume facts. Consolidate exact and semantic "
    "duplicates while preserving every distinct observed fact. Keep the supplied "
    "categories. Do not infer missing dates, employers, titles, technologies, "
    "responsibilities, project outcomes, or experience duration. Never include names, "
    "email addresses, phone numbers, addresses, photographs, links, identifiers, or "
    "other personal contact information. Return only the structured resume details."
)


class ScreenFeedbackDecision(BaseModel):
    should_speak: bool
    confidence: float = Field(ge=0, le=1)
    feedback: str
    reason: str


class ResumeEndState(str, Enum):
    MORE_CONTENT = "more_content"
    APPARENT_END = "apparent_end"
    UNCERTAIN = "uncertain"


class ResumeScrollbarPosition(str, Enum):
    ABOVE_BOTTOM = "above_bottom"
    AT_BOTTOM = "at_bottom"
    NOT_VISIBLE = "not_visible"
    UNCERTAIN = "uncertain"


class ResumeDetails(BaseModel):
    education: list[str] = Field(default_factory=list)
    experience: list[str] = Field(default_factory=list)
    companies: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    projects: list[str] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_lists(cls, data: object) -> object:
        # The vision model sometimes emits null for an empty category.
        if isinstance(data, dict):
            return {
                key: ([] if value is None else value) for key, value in data.items()
            }
        return data


class ResumeViewportObservation(BaseModel):
    visible_sections: list[str] = Field(default_factory=list)
    professional_facts: ResumeDetails = Field(default_factory=ResumeDetails)
    content_clipped_at_bottom: bool
    page_current: int | None = Field(default=None, ge=1)
    page_total: int | None = Field(default=None, ge=1)
    scrollbar_position: ResumeScrollbarPosition
    end_state: ResumeEndState
    end_reason: str = ""
    scroll_instruction: str = ""

    @field_validator("visible_sections", mode="before")
    @classmethod
    def _coerce_null_sections(cls, value: object) -> object:
        return [] if value is None else value

    @field_validator("professional_facts", mode="before")
    @classmethod
    def _coerce_null_facts(cls, value: object) -> object:
        return ResumeDetails() if value is None else value

    @field_validator("end_reason", "scroll_instruction", mode="before")
    @classmethod
    def _coerce_null_text(cls, value: object) -> object:
        # The vision model sometimes emits null instead of an empty string.
        return "" if value is None else value


@dataclass
class ResumeInspectionState:
    details: dict[str, list[str]] = field(
        default_factory=lambda: {key: [] for key in ResumeDetails.model_fields}
    )
    last_viewport_signature: str | None = None
    last_end_state: ResumeEndState | None = None
    last_content_clipped_at_bottom: bool = False
    last_page_current: int | None = None
    last_page_total: int | None = None
    viewport_count: int = 0
    completed_details: ResumeDetails | None = None
    finalized_with_partial_details: bool = False
    finalization_reason: str | None = None

    def accumulated_details(self) -> ResumeDetails:
        return ResumeDetails.model_validate(self.details)

    def merge(self, observation: ResumeViewportObservation) -> None:
        for category, values in observation.professional_facts.model_dump().items():
            existing = self.details[category]
            seen = {_normalize_resume_fact(value) for value in existing}
            for value in values:
                cleaned = " ".join(value.split())
                normalized = _normalize_resume_fact(cleaned)
                if cleaned and normalized not in seen:
                    existing.append(cleaned)
                    seen.add(normalized)

        self.last_end_state = observation.end_state
        self.last_content_clipped_at_bottom = observation.content_clipped_at_bottom
        self.last_page_current = observation.page_current
        self.last_page_total = observation.page_total

    def can_finalize(self) -> bool:
        no_page_counter = (
            self.last_page_current is None and self.last_page_total is None
        )
        page_counter_is_final = (
            self.last_page_current is not None
            and self.last_page_total is not None
            and self.last_page_current == self.last_page_total
        )
        return bool(
            self.last_end_state is ResumeEndState.APPARENT_END
            and not self.last_content_clipped_at_bottom
            and (no_page_counter or page_counter_is_final)
        )


def _normalize_resume_fact(value: str) -> str:
    return " ".join(value.casefold().split())


def _resume_viewport_signature(observation: ResumeViewportObservation) -> str:
    payload = {
        "visible_sections": sorted(
            _normalize_resume_fact(value) for value in observation.visible_sections
        ),
        "professional_facts": {
            category: sorted(_normalize_resume_fact(value) for value in values)
            for category, values in observation.professional_facts.model_dump().items()
        },
        "page_current": observation.page_current,
        "page_total": observation.page_total,
        "content_clipped_at_bottom": observation.content_clipped_at_bottom,
        "scrollbar_position": observation.scrollbar_position.value,
        "end_state": observation.end_state.value,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _resume_viewport_metadata(
    observation: ResumeViewportObservation,
) -> dict[str, object]:
    return {
        "visible_sections": observation.visible_sections,
        "content_clipped_at_bottom": observation.content_clipped_at_bottom,
        "page_current": observation.page_current,
        "page_total": observation.page_total,
        "scrollbar_position": observation.scrollbar_position.value,
        "end_state": observation.end_state.value,
        "end_reason": observation.end_reason,
        "scroll_instruction": observation.scroll_instruction,
    }


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
        timer_enabled: bool = True,
    ) -> None:
        self._room = room
        self._participant_identity = participant_identity
        self._timer_enabled = timer_enabled
        self._llm = openai.LLM.with_openrouter(model=DEFAULT_OPENROUTER_MODEL)
        self._session: AgentSession | None = None
        self._active_question: dict[str, str] | None = None
        self._surface_visible = False
        self._visible_surface: str | None = None
        self._latest_frame: rtc.VideoFrame | None = None
        self._fresh_frame_event = asyncio.Event()
        self._video_stream: rtc.VideoStream | None = None
        self._video_task: asyncio.Task[None] | None = None
        self._timer_task: asyncio.Task[None] | None = None
        self._cleanup_tasks: set[asyncio.Task[None]] = set()
        self._analysis_lock = asyncio.Lock()
        self._resume_capture_active = False
        self._resume_state = ResumeInspectionState()
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

        if self._timer_enabled:
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
        if self._resume_capture_active:
            # The interview plan has started, so the resume phase is over even if
            # it was abandoned before completion. Release the capture so the
            # screen-share subscription is not held for the rest of the session.
            self._resume_capture_active = False
            logger.info(
                "Resume screen capture stopped room=%s reason=question_started",
                self._room.name,
            )
        surface = question.get("surface")
        self._active_question = (
            question if self._timer_enabled and surface in SUPPORTED_SURFACES else None
        )
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
            logger.info(
                "Screen-share track subscribed room=%s track_sid=%s "
                "resume_capture_active=%s",
                self._room.name,
                publication.sid,
                self._resume_capture_active,
            )
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
            logger.info(
                "Screen-share track unsubscribed room=%s track_sid=%s "
                "resume_capture_active=%s",
                self._room.name,
                publication.sid,
                self._resume_capture_active,
            )
            self._latest_frame = None
            self._fresh_frame_event.clear()
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
            self._resume_capture_active
            or (
                question
                and self._surface_visible
                and self._visible_surface == question.get("surface")
            )
        )

    def _sync_screen_subscription(self) -> None:
        if not self._started:
            return
        publication = self._screen_publication()
        if publication is None:
            return
        should_subscribe = self._should_subscribe_to_screen()
        if publication.subscribed != should_subscribe:
            logger.info(
                "Screen-share subscription change requested room=%s track_sid=%s "
                "subscribed=%s resume_capture_active=%s",
                self._room.name,
                publication.sid,
                should_subscribe,
                self._resume_capture_active,
            )
            try:
                publication.set_subscribed(should_subscribe)
            except Exception as exc:
                logger.exception(
                    "Screen-share subscription change failed room=%s track_sid=%s "
                    "subscribed=%s error_type=%s error=%r",
                    self._room.name,
                    publication.sid,
                    should_subscribe,
                    type(exc).__name__,
                    exc,
                )
                raise
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
                    self._fresh_frame_event.set()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Screen-share video stream failed room=%s error_type=%s error=%r",
                    self._room.name,
                    type(exc).__name__,
                    exc,
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
        self._fresh_frame_event.clear()

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

    async def _analyze_resume_frame(
        self,
        frame: rtc.VideoFrame,
    ) -> ResumeViewportObservation:
        accumulated = self._resume_state.accumulated_details().model_dump_json()
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="system", content=RESUME_VIEWPORT_ANALYSIS_PROMPT)
        chat_ctx.add_message(
            role="user",
            content=[
                (
                    "Accumulated professional resume state from earlier viewports: "
                    f"{accumulated}\nInspect the current resume viewport."
                ),
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
            response_format=ResumeViewportObservation,
        ).collect()
        return ResumeViewportObservation.model_validate_json(response.text)

    async def _normalize_resume_details(self) -> ResumeDetails:
        chat_ctx = ChatContext()
        chat_ctx.add_message(role="system", content=RESUME_NORMALIZATION_PROMPT)
        chat_ctx.add_message(
            role="user",
            content=self._resume_state.accumulated_details().model_dump_json(),
        )
        response = await self._llm.chat(
            chat_ctx=chat_ctx,
            response_format=ResumeDetails,
        ).collect()
        return ResumeDetails.model_validate_json(response.text)

    async def _complete_resume_inspection(
        self,
        *,
        use_accumulated_details: bool = False,
        reason: str | None = None,
    ) -> dict[str, object]:
        completion_started_at = time.monotonic()
        if self._resume_capture_active:
            self._resume_capture_active = False
            try:
                self._sync_screen_subscription()
            except Exception:
                logger.exception(
                    "Failed to release resume screen capture room=%s reason=%s",
                    self._room.name,
                    reason or "completion",
                )
            logger.info(
                "Resume screen capture stopped room=%s reason=%s",
                self._room.name,
                reason or "completion",
            )

        completed = self._resume_state.completed_details
        if completed is None:
            if use_accumulated_details:
                completed = self._resume_state.accumulated_details()
                self._resume_state.finalized_with_partial_details = True
                self._resume_state.finalization_reason = reason or "inspection_error"
                logger.warning(
                    "Resume inspection finalized with accumulated details room=%s "
                    "viewports=%d reason=%s",
                    self._room.name,
                    self._resume_state.viewport_count,
                    self._resume_state.finalization_reason,
                )
            else:
                normalization_started_at = time.monotonic()
                try:
                    completed = await self._normalize_resume_details()
                except Exception as exc:
                    logger.exception(
                        "Resume normalization failed room=%s elapsed_ms=%.1f "
                        "error_type=%s error=%r",
                        self._room.name,
                        (time.monotonic() - normalization_started_at) * 1000,
                        type(exc).__name__,
                        exc,
                    )
                    completed = self._resume_state.accumulated_details()
                    self._resume_state.finalized_with_partial_details = True
                    self._resume_state.finalization_reason = "normalization_error"
                else:
                    logger.info(
                        "Resume normalization completed room=%s elapsed_ms=%.1f",
                        self._room.name,
                        (time.monotonic() - normalization_started_at) * 1000,
                    )
            self._resume_state.completed_details = completed

        details_complete = not self._resume_state.finalized_with_partial_details
        if details_complete:
            response_guidance = (
                "The complete professional resume context is now available. Tell the "
                "candidate they can stop screen sharing. Select one recent or relevant "
                "project from resume_details and discuss its purpose, their personal "
                "ownership, one technical challenge, and the result, one question at a "
                "time, before starting the configured interview plan."
            )
        else:
            response_guidance = (
                "Resume inspection ended early, so resume_details contains only the "
                "professional facts successfully extracted before the issue. Tell the "
                "candidate they can stop screen sharing and continue without retrying. "
                "Use the available resume details plus their spoken introduction for the "
                "project discussion. If no project is available, ask them to choose one."
            )

        logger.info(
            "Resume finalization completed room=%s viewports=%d "
            "resume_details_complete=%s reason=%s elapsed_ms=%.1f",
            self._room.name,
            self._resume_state.viewport_count,
            details_complete,
            self._resume_state.finalization_reason,
            (time.monotonic() - completion_started_at) * 1000,
        )

        return {
            "status": "complete",
            "screen_available": (
                self._resume_state.finalization_reason != "screen_share_unavailable"
            ),
            "resume_details": completed.model_dump(),
            "resume_details_complete": details_complete,
            "finalization_reason": self._resume_state.finalization_reason,
            "response_guidance": response_guidance,
        }

    async def inspect_resume_screen(
        self,
        *,
        end_of_document_confirmed: bool = False,
        finish_with_available_details: bool = False,
    ) -> dict[str, object]:
        """Inspect and accumulate one visible resume viewport."""
        inspection_started_at = time.monotonic()
        logger.info(
            "Resume viewport inspection started room=%s viewport=%d "
            "end_of_document_confirmed=%s",
            self._room.name,
            self._resume_state.viewport_count + 1,
            end_of_document_confirmed,
        )

        if self._resume_state.completed_details is not None:
            return await self._complete_resume_inspection()

        if finish_with_available_details:
            async with self._analysis_lock:
                return await self._complete_resume_inspection(
                    use_accumulated_details=True,
                    reason="candidate_stopped_resume_inspection",
                )

        # A confirmation is valid only for an apparent-end result returned by an
        # earlier call. A caller cannot confirm a viewport that has not been inspected.
        if end_of_document_confirmed and self._resume_state.can_finalize():
            async with self._analysis_lock:
                return await self._complete_resume_inspection()

        # Safety net: once enough viewports have been inspected without a
        # confident end, finalize with whatever has been accumulated. Without
        # this the introduction can hang forever re-asking the candidate to
        # scroll a document whose bottom the vision model never confirms.
        if self._resume_state.viewport_count >= RESUME_MAX_VIEWPORTS:
            logger.warning(
                "Resume viewport cap reached; finalizing accumulated details "
                "room=%s viewports=%d elapsed_ms=%.1f",
                self._room.name,
                self._resume_state.viewport_count,
                (time.monotonic() - inspection_started_at) * 1000,
            )
            async with self._analysis_lock:
                return await self._complete_resume_inspection()

        if self._screen_publication() is None:
            logger.warning(
                "Resume screen publication unavailable room=%s elapsed_ms=%.1f",
                self._room.name,
                (time.monotonic() - inspection_started_at) * 1000,
            )
            if self._resume_capture_active or self._resume_state.viewport_count > 0:
                async with self._analysis_lock:
                    return await self._complete_resume_inspection(
                        use_accumulated_details=True,
                        reason="screen_share_unavailable",
                    )
            return self._recovery_result(
                status="screen_share_required",
                candidate_message=RESUME_SCREEN_SHARE_REQUIRED_MESSAGE,
            )

        async with self._analysis_lock:
            if not self._resume_capture_active:
                self._resume_capture_active = True
                logger.info(
                    "Resume screen capture started room=%s", self._room.name
                )
            stage = "subscription"
            stage_started_at = time.monotonic()
            try:
                self._sync_screen_subscription()

                stage = "frame_wait"
                stage_started_at = time.monotonic()
                # The live screen-share stream already holds the current viewport.
                # A screen share stops emitting frames once the candidate stops
                # scrolling, so waiting for a brand-new frame times out on a static
                # page and the viewport is never analyzed. Only block when no frame
                # has arrived yet (e.g. just after the first subscribe); otherwise
                # use the most recent frame, which is the viewport the candidate
                # just scrolled to. Unchanged detection catches a repeated viewport.
                if self._latest_frame is None:
                    self._fresh_frame_event.clear()
                    await asyncio.wait_for(
                        self._fresh_frame_event.wait(),
                        timeout=RESUME_FRAME_WAIT_SECONDS,
                    )
                frame = self._latest_frame
                if frame is None:
                    raise TimeoutError("no video frame available from screen share")

                frame_wait_ms = (time.monotonic() - stage_started_at) * 1000
                logger.info(
                    "Resume frame acquired room=%s viewport=%d elapsed_ms=%.1f",
                    self._room.name,
                    self._resume_state.viewport_count + 1,
                    frame_wait_ms,
                )

                stage = "vision_analysis"
                stage_started_at = time.monotonic()
                observation = await self._analyze_resume_frame(frame)
                vision_analysis_ms = (time.monotonic() - stage_started_at) * 1000
                logger.info(
                    "Resume image analysis completed room=%s viewport=%d "
                    "elapsed_ms=%.1f",
                    self._room.name,
                    self._resume_state.viewport_count + 1,
                    vision_analysis_ms,
                )
            except TimeoutError as exc:
                logger.warning(
                    "Resume viewport inspection timed out room=%s viewport=%d "
                    "stage=%s stage_elapsed_ms=%.1f total_elapsed_ms=%.1f "
                    "error_type=%s error=%r",
                    self._room.name,
                    self._resume_state.viewport_count + 1,
                    stage,
                    (time.monotonic() - stage_started_at) * 1000,
                    (time.monotonic() - inspection_started_at) * 1000,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                return await self._complete_resume_inspection(
                    use_accumulated_details=True,
                    reason=f"{stage}_timeout",
                )
            except Exception as exc:
                logger.exception(
                    "Resume viewport inspection failed room=%s viewport=%d "
                    "stage=%s stage_elapsed_ms=%.1f total_elapsed_ms=%.1f "
                    "error_type=%s error=%r",
                    self._room.name,
                    self._resume_state.viewport_count + 1,
                    stage,
                    (time.monotonic() - stage_started_at) * 1000,
                    (time.monotonic() - inspection_started_at) * 1000,
                    type(exc).__name__,
                    exc,
                )
                return await self._complete_resume_inspection(
                    use_accumulated_details=True,
                    reason=f"{stage}_error",
                )

            self._resume_state.viewport_count += 1

            visible_continuation = bool(
                observation.content_clipped_at_bottom
                or observation.scrollbar_position
                is ResumeScrollbarPosition.ABOVE_BOTTOM
                or (
                    observation.page_current is not None
                    and observation.page_total is not None
                    and observation.page_current < observation.page_total
                )
            )
            if visible_continuation:
                observation.end_state = ResumeEndState.MORE_CONTENT
            elif (observation.page_current is None) != (
                observation.page_total is None
            ) or (
                observation.page_current is not None
                and observation.page_total is not None
                and observation.page_current > observation.page_total
            ):
                observation.end_state = ResumeEndState.UNCERTAIN

            signature = _resume_viewport_signature(observation)
            unchanged = signature == self._resume_state.last_viewport_signature
            self._resume_state.last_viewport_signature = signature

            logger.info(
                "Resume viewport processed room=%s viewport=%d end_state=%s "
                "unchanged=%s clipped_at_bottom=%s page_current=%s page_total=%s "
                "frame_wait_ms=%.1f image_analysis_ms=%.1f total_elapsed_ms=%.1f",
                self._room.name,
                self._resume_state.viewport_count,
                observation.end_state.value,
                unchanged,
                observation.content_clipped_at_bottom,
                observation.page_current,
                observation.page_total,
                frame_wait_ms,
                vision_analysis_ms,
                (time.monotonic() - inspection_started_at) * 1000,
            )

            if unchanged:
                return {
                    "status": "unchanged",
                    "screen_available": True,
                    **_resume_viewport_metadata(observation),
                    "candidate_message": (
                        "I am still seeing the same resume section. Please scroll "
                        "further down, then tell me when the next section is visible."
                    ),
                    "response_guidance": (
                        "Say candidate_message and wait before inspecting again."
                    ),
                }

            self._resume_state.merge(observation)
            if observation.end_state is ResumeEndState.MORE_CONTENT:
                instruction = observation.scroll_instruction.strip() or (
                    "Please scroll further down until the next part of the resume is "
                    "fully visible, then tell me when it is ready."
                )
                return {
                    "status": "more_content",
                    "screen_available": True,
                    **_resume_viewport_metadata(observation),
                    "candidate_message": instruction,
                    "response_guidance": (
                        "Say candidate_message exactly, wait for the candidate to "
                        "scroll and say they are ready, then call inspect_resume_screen "
                        "again with end_of_document_confirmed set to false."
                    ),
                }

            if observation.end_state is ResumeEndState.APPARENT_END:
                return {
                    "status": "apparent_end",
                    "screen_available": True,
                    **_resume_viewport_metadata(observation),
                    "candidate_message": (
                        "Is this the last page or the end of your resume?"
                    ),
                    "response_guidance": (
                        "Ask candidate_message. If the candidate confirms, call "
                        "inspect_resume_screen with end_of_document_confirmed set to "
                        "true. Do not start the project discussion yet."
                    ),
                }

            return {
                "status": "uncertain",
                "screen_available": True,
                **_resume_viewport_metadata(observation),
                "candidate_message": (
                    "I cannot confirm the end of the resume from this view. Is there "
                    "more content below? If there is, please scroll to it. If not, "
                    "please show the very bottom of the last page."
                ),
                "response_guidance": (
                    "Say candidate_message, wait for the candidate to adjust the view, "
                    "then inspect again. Candidate confirmation cannot finalize an "
                    "uncertain or visibly clipped viewport."
                ),
            }

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
            async with self._analysis_lock:
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

        async with self._analysis_lock:
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


def build_resume_inspection_tool(runtime: ScreenFeedbackRuntime):
    @function_tool(
        name="inspect_resume_screen",
        description=(
            "Inspect and accumulate the candidate's shared resume one visible viewport "
            "at a time. Call it after the candidate has shared their screen, opened the "
            "resume, and said the current view is ready. Follow the returned "
            "candidate_message exactly for more_content, unchanged, uncertain, and "
            "apparent_end statuses. After apparent_end, ask whether this is the last "
            "page or end of the resume. Only after the candidate confirms, call this "
            "tool with end_of_document_confirmed set to true. If the candidate stops "
            "sharing or asks to skip the resume, call with finish_with_available_details "
            "set to true. A complete result returns all professional details successfully "
            "accumulated and indicates whether they are complete or partial. Screenshots "
            "and personal contact information are never returned."
        ),
    )
    async def inspect_resume_screen(
        end_of_document_confirmed: bool = False,
        finish_with_available_details: bool = False,
    ) -> dict[str, object]:
        return await runtime.inspect_resume_screen(
            end_of_document_confirmed=end_of_document_confirmed,
            finish_with_available_details=finish_with_available_details,
        )

    return inspect_resume_screen
