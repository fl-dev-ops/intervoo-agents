from __future__ import annotations

import json
import logging
from collections import UserDict
from collections.abc import Mapping
from pathlib import Path
from urllib import error, request
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_PROMPT_AGENT_NAME = "Sara"
DEFAULT_PROMPT_USER_NAME = "the student"
DEFAULT_PROMPT_FETCH_TIMEOUT_SECONDS = 10
AGENT_ROOT = Path(__file__).resolve().parents[1]

_prompt_cache: dict[str, str] = {}


class _SafePromptContext(UserDict[str, object]):
    def __missing__(self, key: str) -> str:
        logger.warning("Prompt placeholder '%s' missing from prompt_context", key)
        return ""


def load_prompt(url: str, *, timeout: float = DEFAULT_PROMPT_FETCH_TIMEOUT_SECONDS) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ValueError("prompt_url must be a non-empty string")

    prompt_source = url.strip()
    cached = _prompt_cache.get(prompt_source)
    if cached is not None:
        return cached

    parsed = urlparse(prompt_source)
    if parsed.scheme in {"http", "https"}:
        logger.info(f"Fetching prompt from {prompt_source}")
        try:
            with request.urlopen(prompt_source, timeout=timeout) as response:
                status = getattr(response, "status", response.getcode())
                if status >= 400:
                    raise RuntimeError(f"Prompt fetch returned HTTP {status}")
                body = response.read().decode("utf-8")
        except error.HTTPError as e:
            raise RuntimeError(
                f"Prompt fetch failed for {prompt_source}: HTTP {e.code} {e.reason}"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Prompt fetch failed for {prompt_source}: {e}") from e
    else:
        prompt_path = Path(prompt_source)
        if not prompt_path.is_absolute():
            prompt_path = AGENT_ROOT / prompt_path
        logger.info(f"Loading prompt from {prompt_path}")
        try:
            body = prompt_path.read_text(encoding="utf-8")
        except Exception as e:
            raise RuntimeError(f"Prompt load failed for {prompt_source}: {e}") from e

    text = body.strip()
    if not text:
        raise ValueError(f"Prompt is empty: {prompt_source}")

    _prompt_cache[prompt_source] = text
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
        "agent_name": DEFAULT_PROMPT_AGENT_NAME,
        "additional_context": "",
        "prompt": "",
        "user_name": (user_name or "").strip() or DEFAULT_PROMPT_USER_NAME,
    }

    if not metadata:
        return prompt_context

    metadata_user_name = metadata.get("user_name")
    if isinstance(metadata_user_name, str) and metadata_user_name.strip():
        prompt_context["user_name"] = metadata_user_name.strip()

    metadata_prompt_context = metadata.get("prompt_context")
    if isinstance(metadata_prompt_context, Mapping):
        additional_context: dict[str, str] = {}
        for key, value in metadata_prompt_context.items():
            if isinstance(key, str) and key:
                string_value = _stringify_prompt_value(value)
                prompt_context[key] = string_value
                if key not in {"agent_name", "user_name"} and string_value:
                    additional_context[key] = string_value

        if additional_context:
            prompt_context["additional_context"] = json.dumps(
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
