from __future__ import annotations

from pathlib import Path
from profile import (
    ProfileError,
    load_profile_catalog,
    parse_profile_catalog,
    pick_profile,
)

import pytest

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "agents.json"


def test_catalog_loads_only_mock_interview() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    assert set(catalog) == {"mock_interview"}


def test_mock_interview_profile_enables_required_tools() -> None:
    profile = load_profile_catalog(CONFIG_PATH)["mock_interview"]

    assert profile.agent_type == "mock-interview-agent"
    assert profile.prompt_url == "prompts/interview/vasanth.md"
    assert profile.voice_speaker == "rohan"
    assert profile.voice_dict_id == "p_fcfdd23b"
    assert profile.end_call_enabled is True
    assert profile.editor_events_enabled is True


def test_pick_profile_resolves_mock_interview() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    profile = pick_profile(catalog, {"agent_id": "mock_interview"})

    assert profile.id == "mock_interview"


def test_pick_profile_rejects_missing_agent_id() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    with pytest.raises(ProfileError, match="agent_id"):
        pick_profile(catalog, {})


def test_pick_profile_rejects_unknown_agent_id() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    with pytest.raises(ProfileError, match="Unknown agent_id"):
        pick_profile(catalog, {"agent_id": "diagnostic"})


def test_parse_catalog_defaults_optional_tools_to_disabled() -> None:
    catalog = parse_profile_catalog(
        {
            "agents": {
                "minimal": {
                    "agent_type": "mock-interview-agent",
                    "prompt_url": "prompts/interview/vasanth.md",
                    "initial_reply": "hi",
                    "voice": {"speaker": "rohan"},
                    "tools": {},
                }
            }
        }
    )

    assert catalog["minimal"].end_call_enabled is False
    assert catalog["minimal"].editor_events_enabled is False
