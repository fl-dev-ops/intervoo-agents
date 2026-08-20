"""Strict contracts for versioned interview modes."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, TypeAlias


class InterviewConfigError(ValueError):
    """Raised when interview configuration violates the public contract."""


class InterviewType(str, Enum):
    MOCK_INTERVIEW = "mock_interview"
    RESUME_MASTERY = "resume_mastery"


class ResumeRound(str, Enum):
    ROUND_1 = "round_1"
    ROUND_2 = "round_2"
    ROUND_3 = "round_3"


@dataclass(frozen=True)
class MockInterviewConfig:
    """Mock Interview v1 intentionally has no client overrides."""


@dataclass(frozen=True)
class ResumeMasteryConfig:
    max_follow_ups: int = 3


ModeConfig: TypeAlias = MockInterviewConfig | ResumeMasteryConfig


@dataclass(frozen=True)
class InterviewRequest:
    type: InterviewType
    version: str
    round: str | None
    config: ModeConfig


@dataclass(frozen=True)
class InterviewAdapters:
    runtime: str
    question_source: str
    progress: str


@dataclass(frozen=True)
class ResumeRoundPolicy:
    id: ResumeRound
    title: str
    angles: tuple[str, ...]


@dataclass(frozen=True)
class InterviewModeSchema:
    """Exact backend contract for one registered interview type/version."""

    prompt_url: str
    scripts: Mapping[str, str]
    adapters: InterviewAdapters
    surfaces: tuple[str, ...]
    tools: tuple[str, ...]
    evaluation: str
    config: Mapping[str, int]
    round_angles: Mapping[str, tuple[str, ...]]
    defaults: ModeConfig


@dataclass(frozen=True)
class InterviewDefinition:
    type: InterviewType
    version: str
    prompt_url: str
    scripts: Mapping[str, str]
    adapters: InterviewAdapters
    surfaces: tuple[str, ...]
    tools: tuple[str, ...]
    evaluation: str
    config: Mapping[str, int]
    rounds: tuple[ResumeRoundPolicy, ...]
    defaults: ModeConfig


@dataclass(frozen=True)
class ResolvedInterview:
    request: InterviewRequest
    definition: InterviewDefinition


def _as_mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise InterviewConfigError(f"{field} must be an object")
    if not all(isinstance(key, str) for key in value):
        raise InterviewConfigError(f"{field} keys must be strings")
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any],
    *,
    allowed: set[str],
    required: set[str],
    field: str,
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise InterviewConfigError(f"{field} contains unknown keys: {unknown}")
    missing = sorted(required - set(value))
    if missing:
        raise InterviewConfigError(f"{field} is missing required keys: {missing}")


def _required_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InterviewConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _parse_type(value: Any, field: str) -> InterviewType:
    raw = _required_str(value, field)
    try:
        return InterviewType(raw)
    except ValueError as error:
        allowed = [item.value for item in InterviewType]
        raise InterviewConfigError(
            f"{field} must be one of {allowed}; got {raw!r}"
        ) from error


def parse_mode_config(interview_type: InterviewType, value: Any) -> ModeConfig:
    raw = _as_mapping(value, "interview.config")
    if interview_type is InterviewType.MOCK_INTERVIEW:
        _reject_unknown_keys(
            raw,
            allowed=set(),
            required=set(),
            field="interview.config",
        )
        return MockInterviewConfig()

    _reject_unknown_keys(
        raw,
        allowed={"max_follow_ups"},
        required=set(),
        field="interview.config",
    )
    max_follow_ups = raw.get("max_follow_ups", 3)
    if (
        isinstance(max_follow_ups, bool)
        or not isinstance(max_follow_ups, int)
        or not 0 <= max_follow_ups <= 3
    ):
        raise InterviewConfigError(
            "interview.config.max_follow_ups must be an integer from 0 through 3"
        )
    return ResumeMasteryConfig(max_follow_ups=max_follow_ups)


def parse_interview_request(value: Any) -> InterviewRequest:
    raw = _as_mapping(value, "interview")
    _reject_unknown_keys(
        raw,
        allowed={"type", "version", "round", "config"},
        required={"type", "version", "config"},
        field="interview",
    )

    interview_type = _parse_type(raw.get("type"), "interview.type")
    version = _required_str(raw.get("version"), "interview.version")
    config = parse_mode_config(interview_type, raw.get("config"))
    raw_round = raw.get("round")

    if interview_type is InterviewType.MOCK_INTERVIEW:
        if "round" in raw:
            raise InterviewConfigError("Mock Interview does not accept a round")
        selected_round = None
    else:
        if "round" not in raw or raw_round is None:
            raise InterviewConfigError(
                "Resume Mastery requires exactly one interview.round"
            )
        round_value = _required_str(raw_round, "interview.round")
        try:
            selected_round = ResumeRound(round_value).value
        except ValueError as error:
            allowed = [item.value for item in ResumeRound]
            raise InterviewConfigError(
                f"interview.round must be one of {allowed}; got {round_value!r}"
            ) from error

    return InterviewRequest(
        type=interview_type,
        version=version,
        round=selected_round,
        config=config,
    )
