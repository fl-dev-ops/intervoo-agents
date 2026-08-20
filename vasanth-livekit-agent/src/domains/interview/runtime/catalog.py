"""Load the backend-owned catalog of versioned interview definitions."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .models import (
    InterviewAdapters,
    InterviewConfigError,
    InterviewDefinition,
    InterviewRoundDefinition,
    InterviewType,
    MockInterviewConfig,
    ResumeMasteryConfig,
    ResumeRound,
    _as_mapping,
    _parse_type,
    _reject_unknown_keys,
    _required_str,
    parse_mode_config,
)

REGISTERED_ADAPTERS = {
    "runtime": {"mock_interview", "resume_mastery"},
    "question_source": {"chroma", "resume_claims"},
    "progress": {"question_store", "resume_rounds"},
}
REGISTERED_SURFACES = {"verbal", "code", "whiteboard", "resume_pdf"}
REGISTERED_TOOLS = {
    "ask_resume_follow_up",
    "build_interview_plan",
    "finish_interview",
    "finish_resume_mastery",
    "get_resume_claim",
    "highlight_code",
    "highlight_whiteboard",
    "list_resume_claims",
    "present_pending_resume_question",
    "read_code_range",
    "read_whiteboard_assessment",
    "start_question",
    "start_resume_question",
}
REGISTERED_EVALUATIONS = {"mock_interview", "none"}
REGISTERED_LIMITS = {
    "main_questions_per_round",
    "max_anchors",
    "max_anchors_per_claim",
    "max_claims",
    "max_document_json_bytes",
    "max_extracted_characters",
    "max_follow_ups_per_main",
    "max_pdf_bytes",
    "max_pdf_pages",
    "max_question_characters",
    "max_rectangles_per_anchor",
    "min_whiteboard_follow_ups",
    "optional_final_round_main_questions",
}
REGISTERED_ANGLES = {
    "problem_scope",
    "ownership",
    "technical_decision",
    "challenge_outcome",
    "baseline_measurement",
    "measurement_method",
    "attribution_confounders",
    "business_engineering_impact",
    "ownership_consistency",
    "alternative_tradeoff",
    "failure_scale",
    "what_would_change",
}

EXPECTED_SCRIPTS = {
    InterviewType.MOCK_INTERVIEW: {"initial_reply"},
    InterviewType.RESUME_MASTERY: {
        "opening",
        "round_2_transition",
        "round_3_transition",
        "verified_not_found",
        "viewer_recovery",
        "cannot_locate",
        "closing",
    },
}


@dataclass(frozen=True)
class InterviewCatalog:
    definitions: Mapping[tuple[InterviewType, str], InterviewDefinition]

    def require(
        self, interview_type: InterviewType, version: str
    ) -> InterviewDefinition:
        definition = self.definitions.get((interview_type, version))
        if definition is None:
            known = sorted(
                f"{known_type.value}/{known_version}"
                for known_type, known_version in self.definitions
            )
            raise InterviewConfigError(
                f"Unknown interview {interview_type.value}/{version}; known: {known}"
            )
        return definition


def _parse_registered_name(value: Any, field: str, registered: set[str]) -> str:
    name = _required_str(value, field)
    if name not in registered:
        raise InterviewConfigError(
            f"{field} references unregistered key {name!r}; known: {sorted(registered)}"
        )
    return name


def _parse_string_map(value: Any, field: str) -> dict[str, str]:
    raw = _as_mapping(value, field)
    parsed: dict[str, str] = {}
    for key, item in raw.items():
        parsed[key] = _required_str(item, f"{field}.{key}")
    return parsed


def _parse_registered_list(value: Any, field: str, registered: set[str]) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise InterviewConfigError(f"{field} must be an array")
    parsed = tuple(
        _parse_registered_name(item, f"{field}[{index}]", registered)
        for index, item in enumerate(value)
    )
    if len(parsed) != len(set(parsed)):
        raise InterviewConfigError(f"{field} must not contain duplicates")
    return parsed


def _parse_adapters(value: Any) -> InterviewAdapters:
    raw = _as_mapping(value, "interview definition.adapters")
    expected = set(REGISTERED_ADAPTERS)
    _reject_unknown_keys(
        raw,
        allowed=expected,
        required=expected,
        field="interview definition.adapters",
    )
    return InterviewAdapters(
        runtime=_parse_registered_name(
            raw["runtime"],
            "interview definition.adapters.runtime",
            REGISTERED_ADAPTERS["runtime"],
        ),
        question_source=_parse_registered_name(
            raw["question_source"],
            "interview definition.adapters.question_source",
            REGISTERED_ADAPTERS["question_source"],
        ),
        progress=_parse_registered_name(
            raw["progress"],
            "interview definition.adapters.progress",
            REGISTERED_ADAPTERS["progress"],
        ),
    )


def _parse_limits(value: Any) -> Mapping[str, int]:
    raw = _as_mapping(value, "interview definition.limits")
    unknown = sorted(set(raw) - REGISTERED_LIMITS)
    if unknown:
        raise InterviewConfigError(
            f"interview definition.limits contains unknown keys: {unknown}"
        )

    parsed: dict[str, int] = {}
    for key, item in raw.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise InterviewConfigError(
                f"interview definition.limits.{key} must be a non-negative integer"
            )
        parsed[key] = item
    return MappingProxyType(parsed)


def _parse_round(value: Any, index: int) -> InterviewRoundDefinition:
    field = f"interview definition.rounds[{index}]"
    raw = _as_mapping(value, field)
    _reject_unknown_keys(
        raw,
        allowed={"id", "title", "angles"},
        required={"id", "title", "angles"},
        field=field,
    )
    round_value = _required_str(raw["id"], f"{field}.id")
    try:
        round_id = ResumeRound(round_value)
    except ValueError as error:
        raise InterviewConfigError(
            f"{field}.id must be a registered Resume round; got {round_value!r}"
        ) from error

    angles = _parse_registered_list(
        raw["angles"], f"{field}.angles", REGISTERED_ANGLES
    )
    if not angles:
        raise InterviewConfigError(f"{field}.angles must not be empty")
    return InterviewRoundDefinition(
        id=round_id,
        title=_required_str(raw["title"], f"{field}.title"),
        angles=angles,
    )


def parse_interview_definition(value: Any) -> InterviewDefinition:
    raw = _as_mapping(value, "interview definition")
    keys = {
        "type",
        "version",
        "prompt_url",
        "scripts",
        "adapters",
        "surfaces",
        "tools",
        "evaluation",
        "limits",
        "rounds",
        "defaults",
    }
    _reject_unknown_keys(
        raw,
        allowed=keys,
        required=keys,
        field="interview definition",
    )

    interview_type = _parse_type(raw["type"], "interview definition.type")
    version = _required_str(raw["version"], "interview definition.version")
    scripts = _parse_string_map(raw["scripts"], "interview definition.scripts")
    expected_scripts = EXPECTED_SCRIPTS[interview_type]
    _reject_unknown_keys(
        scripts,
        allowed=expected_scripts,
        required=expected_scripts,
        field="interview definition.scripts",
    )
    rounds_value = raw["rounds"]
    if not isinstance(rounds_value, list):
        raise InterviewConfigError("interview definition.rounds must be an array")
    rounds = tuple(_parse_round(item, index) for index, item in enumerate(rounds_value))
    if len(rounds) != len({item.id for item in rounds}):
        raise InterviewConfigError("interview definition.rounds contains duplicate ids")

    defaults = parse_mode_config(interview_type, raw["defaults"])
    if interview_type is InterviewType.MOCK_INTERVIEW and rounds:
        raise InterviewConfigError("Mock Interview definition must not declare rounds")
    if interview_type is InterviewType.RESUME_MASTERY:
        expected_rounds = tuple(ResumeRound)
        if tuple(item.id for item in rounds) != expected_rounds:
            raise InterviewConfigError(
                "Resume Mastery rounds must be round_1, round_2, and round_3 in order"
            )
        if not isinstance(defaults, ResumeMasteryConfig):
            raise InterviewConfigError("Resume Mastery defaults are invalid")
    elif not isinstance(defaults, MockInterviewConfig):
        raise InterviewConfigError("Mock Interview defaults are invalid")

    return InterviewDefinition(
        type=interview_type,
        version=version,
        prompt_url=_required_str(
            raw["prompt_url"], "interview definition.prompt_url"
        ),
        scripts=MappingProxyType(scripts),
        adapters=_parse_adapters(raw["adapters"]),
        surfaces=_parse_registered_list(
            raw["surfaces"], "interview definition.surfaces", REGISTERED_SURFACES
        ),
        tools=_parse_registered_list(
            raw["tools"], "interview definition.tools", REGISTERED_TOOLS
        ),
        evaluation=_parse_registered_name(
            raw["evaluation"],
            "interview definition.evaluation",
            REGISTERED_EVALUATIONS,
        ),
        limits=_parse_limits(raw["limits"]),
        rounds=rounds,
        defaults=defaults,
    )


def load_interview_catalog(path: str | Path) -> InterviewCatalog:
    config_dir = Path(path)
    if not config_dir.is_dir():
        raise InterviewConfigError(f"Interview config directory not found: {config_dir}")

    definitions: dict[tuple[InterviewType, str], InterviewDefinition] = {}
    files = sorted(config_dir.glob("*.json"))
    if not files:
        raise InterviewConfigError(f"No interview definitions found in {config_dir}")

    for config_path in files:
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as error:
            raise InterviewConfigError(
                f"Invalid interview JSON: {config_path}"
            ) from error

        definition = parse_interview_definition(payload)
        expected_name = f"{definition.type.value}.{definition.version}.json"
        if config_path.name != expected_name:
            raise InterviewConfigError(
                f"Interview definition filename must be {expected_name}; got {config_path.name}"
            )
        key = (definition.type, definition.version)
        if key in definitions:
            raise InterviewConfigError(
                f"Duplicate interview definition: {definition.type.value}/{definition.version}"
            )
        definitions[key] = definition

    return InterviewCatalog(definitions=MappingProxyType(definitions))
