from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from prompt import (
    build_prompt_context,
    clear_prompt_cache,
    load_prompt,
    render_prompt,
)


@pytest.fixture(autouse=True)
def _reset_prompt_cache():
    clear_prompt_cache()
    yield
    clear_prompt_cache()


def test_build_prompt_context_uses_defaults_when_metadata_missing() -> None:
    context = build_prompt_context(None)

    assert context == {
        "agentName": "Sara",
        "additionalContext": "",
        "userName": "the student",
    }


def test_build_prompt_context_overrides_user_name_from_metadata() -> None:
    context = build_prompt_context({"userName": "Ravi"})

    assert context["userName"] == "Ravi"


def test_build_prompt_context_packages_extra_keys_into_additional_context() -> None:
    context = build_prompt_context(
        {
            "userName": "Ravi",
            "prompt_context": {
                "comfortableLanguage": "hindi",
                "score": 42,
            },
        }
    )

    assert context["userName"] == "Ravi"
    assert context["comfortableLanguage"] == "hindi"
    assert context["score"] == "42"
    assert context["additionalContext"] == (
        '{"comfortableLanguage": "hindi", "score": "42"}'
    )


def test_render_prompt_substitutes_known_keys_and_warns_on_missing() -> None:
    template = "Hello {userName}, I am {agentName}. Note: {missingKey}"

    rendered = render_prompt(
        template,
        context={"userName": "Ravi", "agentName": "Sara"},
    )

    assert rendered.startswith("Hello Ravi, I am Sara.")
    assert "{missingKey}" not in rendered


def _fake_response(body: bytes, status: int = 200):
    response = io.BytesIO(body)
    response.status = status
    response.getcode = lambda: status
    return response


def test_load_prompt_fetches_url_and_caches() -> None:
    body = b"You are {agentName}. Talk to {userName}."

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

    assert first == "You are {agentName}. Talk to {userName}."
    assert first == second
    assert mock_urlopen.call_count == 1


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
