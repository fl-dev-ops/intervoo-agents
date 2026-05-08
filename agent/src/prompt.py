from __future__ import annotations

import json
import logging
from collections import UserDict
from collections.abc import Mapping
from urllib import error, request

logger = logging.getLogger(__name__)

DEFAULT_PROMPT_AGENT_NAME = "Sara"
DEFAULT_PROMPT_USER_NAME = "the student"
DEFAULT_PROMPT_FETCH_TIMEOUT_SECONDS = 10

_prompt_cache: dict[str, str] = {}


class _SafePromptContext(UserDict[str, object]):
    def __missing__(self, key: str) -> str:
        logger.warning("Prompt placeholder '%s' missing from prompt_context", key)
        return ""


def load_prompt(url: str, *, timeout: float = DEFAULT_PROMPT_FETCH_TIMEOUT_SECONDS) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("prompt_url must be a non-empty string")

    cached = _prompt_cache.get(url)
    if cached is not None:
        return cached

    logger.info(f"Fetching prompt from {url}")
    try:
        with request.urlopen(url, timeout=timeout) as response:
            status = getattr(response, "status", response.getcode())
            if status >= 400:
                raise RuntimeError(f"Prompt fetch returned HTTP {status}")
            body = response.read().decode("utf-8")
    except error.HTTPError as e:
        raise RuntimeError(f"Prompt fetch failed for {url}: HTTP {e.code} {e.reason}") from e
    except Exception as e:
        raise RuntimeError(f"Prompt fetch failed for {url}: {e}") from e

    text = body.strip()
    if not text:
        raise ValueError(f"Fetched prompt is empty: {url}")

    _prompt_cache[url] = text
    return text


def clear_prompt_cache() -> None:
    _prompt_cache.clear()


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
    template: str,
    *,
    context: Mapping[str, object] | None = None,
) -> str:
    safe_context = _SafePromptContext(
        {key: _stringify_prompt_value(value) for key, value in (context or {}).items()}
    )
    return template.format_map(safe_context).strip()
