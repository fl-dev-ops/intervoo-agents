from __future__ import annotations

import io
from pathlib import Path
from profile import load_profile_catalog
from unittest.mock import patch

import pytest

from prompt import (
    build_prompt_context,
    clear_prompt_cache,
    load_prompt,
    render_prompt,
)

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "agents.json"


@pytest.fixture(autouse=True)
def _reset_prompt_cache():
    clear_prompt_cache()
    yield
    clear_prompt_cache()


def test_build_prompt_context_uses_defaults_when_metadata_missing() -> None:
    context = build_prompt_context(None)

    assert context == {
        "agent_name": "Sara",
        "additional_context": "",
        "prompt": "",
        "question_filters": "{}",
        "interview_questions": "",
        "user_name": "the student",
        "user_details": "",
    }


def test_build_prompt_context_overrides_user_name_from_metadata() -> None:
    context = build_prompt_context({"user_name": "Ravi"})

    assert context["user_name"] == "Ravi"


def test_build_prompt_context_packages_extra_keys_into_additional_context() -> None:
    context = build_prompt_context(
        {
            "user_name": "Ravi",
            "prompt_context": {
                "comfortable_language": "hindi",
                "score": 42,
            },
        }
    )

    assert context["user_name"] == "Ravi"
    assert context["comfortable_language"] == "hindi"
    assert context["score"] == "42"
    assert context["additional_context"] == (
        '{"comfortable_language": "hindi", "score": "42"}'
    )


def test_build_prompt_context_includes_question_filters_from_metadata() -> None:
    context = build_prompt_context(
        {
            "question_filters": {
                "content_type": "diagnostic_question",
                "domain": "computer_science",
                "category": "domain",
                "band": 3,
            },
        }
    )

    assert context["question_filters"] == (
        '{"band": 3, "category": "domain", '
        '"content_type": "diagnostic_question", "domain": "computer_science"}'
    )


def test_render_prompt_substitutes_known_keys_and_warns_on_missing() -> None:
    template = "Hello {user_name}, I am {agent_name}. Note: {missing_key}"

    rendered = render_prompt(
        template,
        context={"user_name": "Ravi", "agent_name": "Sara"},
    )

    assert rendered.startswith("Hello Ravi, I am Sara.")
    assert "{missing_key}" not in rendered


def _fake_response(body: bytes, status: int = 200):
    response = io.BytesIO(body)
    response.status = status
    response.getcode = lambda: status
    return response


def test_load_prompt_fetches_url_and_caches() -> None:
    body = b"You are {agent_name}. Talk to {user_name}."

    class _Manager:
        def __init__(self, payload):
            self._payload = payload

        def __enter__(self):
            return self._payload

        def __exit__(self, *args):
            return False

    with patch("prompt.request.urlopen") as mock_urlopen:
        mock_urlopen.return_value = _Manager(_fake_response(body))

        first = load_prompt("https://example.com/p.md")
        second = load_prompt("https://example.com/p.md")

    assert first == "You are {agent_name}. Talk to {user_name}."
    assert first == second
    assert mock_urlopen.call_count == 1


def test_load_prompt_reads_local_prompt_relative_to_agent_root() -> None:
    prompt = load_prompt("prompts/diagnostic/v1.md")

    assert "structured technical diagnostic interview" in prompt


def test_configured_profile_prompts_load_from_local_files() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    prompts = {
        agent_id: load_prompt(profile.prompt_url)
        for agent_id, profile in catalog.items()
    }

    assert set(prompts) == {"interview", "pre_screen", "diagnostic", "diagnostic_v2", "job"}
    assert "Job Interview Voice Agent" in prompts["job"]
    assert "Interview Practice Voice Agent" in prompts["interview"]
    assert "Role And Objective" in prompts["pre_screen"]
    assert "Diagnostic Interview Agent Prompt" in prompts["diagnostic"]


def test_load_prompt_raises_on_empty_body() -> None:
    class _Manager:
        def __enter__(self):
            return _fake_response(b"   \n  ")

        def __exit__(self, *args):
            return False

    with patch("prompt.request.urlopen", return_value=_Manager()):
        with pytest.raises(ValueError, match="empty"):
            load_prompt("https://example.com/empty.md")


def test_load_prompt_raises_on_http_error() -> None:
    class _Manager:
        def __enter__(self):
            return _fake_response(b"server down", status=500)

        def __exit__(self, *args):
            return False

    with patch("prompt.request.urlopen", return_value=_Manager()):
        with pytest.raises(RuntimeError, match="HTTP 500"):
            load_prompt("https://example.com/broken.md")
