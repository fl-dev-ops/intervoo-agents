from __future__ import annotations

import json
import logging
from collections import UserDict
from collections.abc import Mapping
from pathlib import Path

from constants import (
    DEFAULT_PROMPT_AGENT_NAME,
    DEFAULT_PROMPT_USER_NAME,
    PROMPT_PATH,
)

logger = logging.getLogger("interview_coaching_agent")

KNOWLEDGE_BASE_ENABLED_INSTRUCTIONS = """
## 8B. KNOWLEDGE BASE QUESTION RETRIEVAL

The full question bank is not loaded into your context. Use retrieve_knowledge to retrieve assessment-question records when you have useful stage and conversation context.

Retrieved records contain:
  - id: unique record identifier; use this as question_id
  - text: the question text to ask
  - metadata: structured fields for routing and sequencing

Expected metadata:
  - category: one of "opening" | "domain" | "behavioral" | "closing"
  - difficulty_level: one of "easy" | "medium" | "hard"
  - topic: optional subject area
  - band: assessment band
  - question_type: optional list of dimensions such as "Thinking", "Language", "Confidence"

Tool rules:
  - Call retrieve_knowledge before asking questions from a new assessment state.
  - Use a query that includes the current state and useful conversation context.
  - Use filters for known hard constraints such as content_type, domain, category, difficulty_level, and band.
  - Always include content_type = "diagnostic_question" and domain = "computer_science" in filters.
  - Keep track of asked record ids and pass them as exclude_ids.
  - If retrieve_knowledge returns status = "unavailable" or "empty", explain that the assessment cannot continue reliably right now and call end_call.
  - Only ask questions returned by retrieve_knowledge. Do not generate or substitute questions from memory.

Stage retrieval:
  - Opening: retrieve category = "opening"; choose exactly 3 records: 1 easy, 1 medium, 1 hard; ask easy first and hard last.
  - Domain: after project discovery, retrieve category = "domain" using the student's stated stack, project, and domain in the query. If answers are vague, use OOP Principles, Database and SQL, REST API Concepts, OS Fundamentals, and Data Structures as query context.
  - Behavioral: retrieve category = "behavioral" and ask returned records in order.
  - Closing: retrieve category = "closing" and ask returned records in order before the final fixed question.
""".strip()

KNOWLEDGE_BASE_DISABLED_INSTRUCTIONS = """
## 8B. KNOWLEDGE BASE DISABLED

Knowledge base retrieval is disabled for this runtime. You do not have access to external assessment-question records, and you must not call any retrieval tool.

Do not invent assessment questions. If the assessment reaches a point where external question records are required, explain that the assessment cannot continue reliably right now and call end_call.
""".strip()


class _SafePromptContext(UserDict[str, object]):
    def __missing__(self, key: str) -> str:
        logger.warning("Prompt placeholder '%s' missing from prompt_context", key)
        return ""


def load_prompt(path: Path = PROMPT_PATH) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")

    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise ValueError(f"Prompt file is empty: {path}")

    return prompt


def _stringify_prompt_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def build_prompt_context(
    metadata: Mapping[str, object] | None,
    *,
    user_name: str | None = None,
    knowledge_base_enabled: bool = True,
) -> dict[str, str]:
    prompt_context: dict[str, str] = {
        "agentName": DEFAULT_PROMPT_AGENT_NAME,
        "additionalContext": "",
        "knowledgeBaseInstructions": (
            KNOWLEDGE_BASE_ENABLED_INSTRUCTIONS
            if knowledge_base_enabled
            else KNOWLEDGE_BASE_DISABLED_INSTRUCTIONS
        ),
        "userName": (user_name or "").strip() or DEFAULT_PROMPT_USER_NAME,
    }

    if not metadata:
        return prompt_context

    metadata_user_name = metadata.get("userName") or metadata.get("user_name")
    if isinstance(metadata_user_name, str) and metadata_user_name.strip():
        prompt_context["userName"] = metadata_user_name.strip()

    metadata_prompt_context = metadata.get("prompt_context") or metadata.get(
        "promptContext"
    )
    if isinstance(metadata_prompt_context, Mapping):
        additional_context: dict[str, str] = {}
        for key, value in metadata_prompt_context.items():
            if isinstance(key, str) and key:
                string_value = _stringify_prompt_value(value)
                prompt_context[key] = string_value
                if key not in {"agentName", "userName"} and string_value:
                    additional_context[key] = string_value

        if additional_context:
            prompt_context["additionalContext"] = json.dumps(
                additional_context, ensure_ascii=True, sort_keys=True
            )

    return prompt_context


def render_prompt(
    template: str | None = None,
    *,
    context: Mapping[str, object] | None = None,
) -> str:
    prompt_template = load_prompt() if template is None else template
    safe_context = _SafePromptContext(
        {key: _stringify_prompt_value(value) for key, value in (context or {}).items()}
    )
    return prompt_template.format_map(safe_context).strip()
