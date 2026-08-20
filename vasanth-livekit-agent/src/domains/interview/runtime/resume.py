"""Resume Mastery runtime adapter."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from functools import partial
from typing import Any

from domains.interview.resume import (
    ResumeDocumentLimits,
    ResumeDocumentRepository,
    ResumeProgressPhase,
    SelectedRoundProgress,
    parse_resume_artifact_reference,
)
from infrastructure.prompt import build_prompt_context
from infrastructure.storage import build_s3_json_read_config, read_s3_json_object
from services.agent.resume import ResumeMasteryAgent
from tools.interview.resume_questions import (
    ResumeQuestionController,
    build_resume_question_tools,
)
from tools.interview.resume_read import build_resume_read_tools
from tools.interview.resume_rpc import ResumeRpcClient

from .factory import InterviewRuntimeError, RuntimeServices
from .models import (
    InterviewType,
    ResolvedInterview,
    ResumeMasteryConfig,
    ResumeRound,
)

logger = logging.getLogger(__name__)


class ResumeMasteryRuntime:
    """Own the selected Resume round, repository, scripts, tools, and progress."""

    content_tracing_enabled = False
    accepts_frontend_questions = False
    uses_mock_pipeline = False
    uses_editor_events = False

    def __init__(
        self,
        *,
        resolved: ResolvedInterview,
        metadata: Mapping[str, object],
    ) -> None:
        expected_metadata_keys = {"agent_id", "user_name", "interview", "resume"}
        if set(metadata) != expected_metadata_keys:
            raise InterviewRuntimeError(
                "Resume Mastery private metadata has an invalid shape"
            )
        if metadata.get("agent_id") != "mock_interview":
            raise InterviewRuntimeError(
                "Resume Mastery requires the mock_interview agent profile"
            )
        user_name = metadata.get("user_name")
        if (
            not isinstance(user_name, str)
            or user_name != user_name.strip()
            or not 1 <= len(user_name) <= 60
        ):
            raise InterviewRuntimeError(
                "Resume Mastery requires a valid user_name"
            )
        if resolved.request.type is not InterviewType.RESUME_MASTERY:
            raise InterviewRuntimeError(
                "Resume adapter received a non-Resume interview"
            )
        if "questions" in metadata:
            raise InterviewRuntimeError(
                "Resume Mastery does not accept frontend-supplied questions"
            )
        if not isinstance(resolved.request.config, ResumeMasteryConfig):
            raise InterviewRuntimeError("Resume Mastery config was not resolved")
        if resolved.request.round is None:
            raise InterviewRuntimeError("Resume Mastery requires one selected round")

        self.resolved = resolved
        self.prompt_url = resolved.definition.prompt_url
        self.initial_reply = ""
        self.selected_round = ResumeRound(resolved.request.round)
        self._metadata = metadata
        policy = next(
            (
                item
                for item in resolved.definition.rounds
                if item.id is self.selected_round
            ),
            None,
        )
        if policy is None:
            raise InterviewRuntimeError("Selected Resume round has no policy")

        reference = parse_resume_artifact_reference(metadata)
        limits = ResumeDocumentLimits.from_config_limits(
            resolved.definition.limits
        )
        s3_config = build_s3_json_read_config()
        self.repository = ResumeDocumentRepository(
            reference=reference,
            limits=limits,
            read_json_object=partial(read_s3_json_object, s3_config),
        )
        self.progress = SelectedRoundProgress.from_limits(
            selected_round=self.selected_round,
            angle_ids=policy.angles,
            limits=resolved.definition.limits,
            max_follow_ups=resolved.request.config.max_follow_ups,
        )
        self._scripts = resolved.definition.scripts
        self._max_question_characters = resolved.definition.limits[
            "max_question_characters"
        ]
        self._controller: ResumeQuestionController | None = None

    async def prepare(self) -> None:
        started = asyncio.get_running_loop().time()
        logger.info(
            "[EXT-API:resume-runtime] action=load status=started mode=%s "
            "version=%s round=%s count=0 elapsed_ms=0",
            self.resolved.request.type.value,
            self.resolved.request.version,
            self.selected_round.value,
        )
        await asyncio.to_thread(self.repository.load)
        eligible = self.repository.list_eligible_claims(self.selected_round)
        if not eligible:
            raise InterviewRuntimeError(
                "Resume document has no eligible claim for the selected round"
            )
        logger.info(
            "[EXT-API:resume-runtime] action=load status=completed mode=%s "
            "version=%s round=%s count=%d elapsed_ms=%d",
            self.resolved.request.type.value,
            self.resolved.request.version,
            self.selected_round.value,
            len(eligible),
            round((asyncio.get_running_loop().time() - started) * 1000),
        )

    def build_prompt_context(
        self, metadata: Mapping[str, object]
    ) -> dict[str, str]:
        context = build_prompt_context(metadata)
        return {"user_name": context["user_name"]}

    def build_tools(self, services: RuntimeServices) -> list[Any]:
        rpc = ResumeRpcClient(
            room=services.ctx.room,
            participant_identity=services.participant_identity,
            document_sha256=self.repository.document.source.pdf_sha256,
        )
        self._controller = ResumeQuestionController(
            repository=self.repository,
            progress=self.progress,
            rpc=rpc,
            scripts=self._scripts,
            max_question_characters=self._max_question_characters,
            close_room=services.close_room,
            shutdown_job=services.shutdown_job,
        )
        return [
            *build_resume_read_tools(
                repository=self.repository,
                progress=self.progress,
            ),
            *build_resume_question_tools(self._controller),
        ]

    def build_agent(
        self,
        *,
        instructions: str,
        tools: list[Any],
        prompt_context: Mapping[str, str],
        participant_identity: str,
        room_name: str,
    ) -> ResumeMasteryAgent:
        scripts = [
            self._scripts["opening"].format(
                user_name=prompt_context["user_name"]
            )
        ]
        if self.selected_round is ResumeRound.ROUND_2:
            scripts.append(self._scripts["round_2_transition"])
        elif self.selected_round is ResumeRound.ROUND_3:
            scripts.append(self._scripts["round_3_transition"])
        return ResumeMasteryAgent(
            instructions=instructions,
            tools=tools,
            initial_scripts=tuple(scripts),
            participant_identity=participant_identity,
            room_name=room_name,
        )

    def on_conversation_item(self, item: object) -> None:
        if self._controller is not None:
            self._controller.on_conversation_item(item)

    def report(self) -> dict[str, object]:
        snapshot = self.progress.snapshot()
        return {
            "interview": {
                "type": self.resolved.request.type.value,
                "version": self.resolved.request.version,
                "round": snapshot.selected_round.value,
                "outcome": (
                    "complete"
                    if snapshot.phase is ResumeProgressPhase.FINISHED
                    else "incomplete"
                ),
                "main_question_count": snapshot.main_question_count,
                "follow_up_count": snapshot.follow_up_count,
            }
        }
