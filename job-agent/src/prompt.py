from __future__ import annotations

import json
import logging
from collections import UserDict
from collections.abc import Mapping
from pathlib import Path

from constants import DEFAULT_PROMPT_AGENT_NAME, DEFAULT_PROMPT_USER_NAME, PROMPT_PATH

logger = logging.getLogger("job_finder_agent")


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
) -> dict[str, str]:
    prompt_context: dict[str, str] = {
        "agentName": DEFAULT_PROMPT_AGENT_NAME,
        "additionalContext": "",
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
