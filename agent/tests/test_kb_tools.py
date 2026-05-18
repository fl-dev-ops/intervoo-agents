from __future__ import annotations

from kb_tools import merge_diagnostic_filters


def test_merge_diagnostic_filters_uses_session_defaults_when_tool_filters_missing() -> None:
    filters = merge_diagnostic_filters(
        {
            "content_type": "diagnostic_question",
            "domain": "computer_science",
            "category": "domain",
            "band": 2,
        },
        {
            "content_type": None,
            "domain": None,
            "category": None,
            "difficulty_level": "easy",
            "band": None,
        },
    )

    assert filters == {
        "content_type": "diagnostic_question",
        "domain": "computer_science",
        "category": "domain",
        "difficulty_level": "easy",
        "band": 2,
    }


def test_merge_diagnostic_filters_prefers_explicit_tool_filters() -> None:
    filters = merge_diagnostic_filters(
        {
            "content_type": "diagnostic_question",
            "domain": "computer_science",
            "category": "domain",
            "band": 2,
        },
        {
            "content_type": None,
            "domain": None,
            "category": "behavioral",
            "difficulty_level": None,
            "band": 3,
        },
    )

    assert filters == {
        "content_type": "diagnostic_question",
        "domain": "computer_science",
        "category": "behavioral",
        "band": 3,
    }


def test_merge_diagnostic_filters_returns_none_without_any_filters() -> None:
    filters = merge_diagnostic_filters(
        None,
        {
            "content_type": None,
            "domain": None,
            "category": None,
            "difficulty_level": None,
            "band": None,
        },
    )

    assert filters is None
