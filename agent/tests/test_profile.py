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


def test_catalog_loads_all_four_personas() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    assert set(catalog.keys()) == {"interview", "pre_screen", "diagnostic", "job"}


def test_pre_screen_profile_has_end_call_and_no_kb() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)
    profile = catalog["pre_screen"]

    assert profile.agent_type == "pre-screen-agent"
    assert profile.prompt_url == "prompts/pre-screen/v1.md"
    assert profile.voice_speaker == "ishita"
    assert profile.voice_dict_id == "p_fcfdd23b"
    assert profile.end_call_enabled is True
    assert profile.kb_collection is None
    assert profile.kb_shape == "simple"
    assert profile.memory_enabled is False


def test_diagnostic_profile_has_diagnostic_kb_shape() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)
    profile = catalog["diagnostic"]

    assert profile.prompt_url == "prompts/diagnostic/v1.md"
    assert profile.kb_shape == "diagnostic"
    assert profile.end_call_enabled is True
    assert profile.memory_enabled is False


def test_job_profile_has_memory_and_no_end_call() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)
    profile = catalog["job"]

    assert profile.prompt_url == "prompts/job/v1.md"
    assert profile.voice_speaker == "ritu"
    assert profile.end_call_enabled is False
    assert profile.memory_enabled is True


def test_interview_profile_has_no_end_call_no_memory() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)
    profile = catalog["interview"]

    assert profile.prompt_url == "prompts/interview/v1.md"
    assert profile.voice_speaker == "kavya"
    assert profile.end_call_enabled is False
    assert profile.kb_collection is None
    assert profile.memory_enabled is False


def test_pick_profile_resolves_by_metadata_agent_id() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    profile = pick_profile(catalog, {"agent_id": "diagnostic"})

    assert profile.id == "diagnostic"


def test_pick_profile_rejects_camel_case_agent_id() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    with pytest.raises(ProfileError, match="agent_id"):
        pick_profile(catalog, {"agentId": "job"})


def test_pick_profile_rejects_missing_agent_id() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    with pytest.raises(ProfileError, match="agent_id"):
        pick_profile(catalog, {})


def test_pick_profile_rejects_unknown_agent_id() -> None:
    catalog = load_profile_catalog(CONFIG_PATH)

    with pytest.raises(ProfileError, match="Unknown agent_id"):
        pick_profile(catalog, {"agent_id": "nope"})


def test_parse_catalog_rejects_missing_kb_collection() -> None:
    with pytest.raises(ProfileError, match=r"knowledge_base\.collection"):
        parse_profile_catalog(
            {
                "agents": {
                    "bad": {
                        "agent_type": "bad-agent",
                        "prompt_url": "https://example.com/p.md",
                        "initial_reply": "hi",
                        "voice": {"speaker": "ishita"},
                        "tools": {"knowledge_base": {}},
                    }
                }
            }
        )


def test_parse_catalog_rejects_unknown_kb_shape() -> None:
    with pytest.raises(ProfileError, match=r"knowledge_base\.shape"):
        parse_profile_catalog(
            {
                "agents": {
                    "bad": {
                        "agent_type": "bad-agent",
                        "prompt_url": "https://example.com/p.md",
                        "initial_reply": "hi",
                        "voice": {"speaker": "ishita"},
                        "tools": {
                            "knowledge_base": {
                                "collection": "x",
                                "shape": "exotic",
                            }
                        },
                    }
                }
            }
        )


def test_parse_catalog_disables_kb_when_tools_omits_it() -> None:
    catalog = parse_profile_catalog(
        {
            "agents": {
                "minimal": {
                    "agent_type": "x",
                    "prompt_url": "https://example.com/p.md",
                    "initial_reply": "hi",
                    "voice": {"speaker": "ishita"},
                    "tools": {},
                }
            }
        }
    )

    assert catalog["minimal"].kb_collection is None
    assert catalog["minimal"].kb_shape == "simple"
    assert catalog["minimal"].end_call_enabled is False
    assert catalog["minimal"].memory_enabled is False
