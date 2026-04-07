from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import (
    AGENT_NAME,
    DEFAULT_DEEPGRAM_STT_LANGUAGE,
    DEFAULT_DEEPGRAM_STT_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_SARVAM_TTS_LANGUAGE,
    DEFAULT_SARVAM_TTS_MODEL,
    DEFAULT_SARVAM_TTS_SPEAKER,
    PROMPT_PATH,
    JobFinderAgent,
    SessionMode,
    build_agent_session,
    build_runtime_config,
    load_prompt,
    parse_room_metadata,
    resolve_session_mode,
)
from memory import resolve_user_id_from_room_metadata


def test_load_prompt_reads_prompt_file() -> None:
    prompt = load_prompt()

    assert prompt == PROMPT_PATH.read_text(encoding="utf-8").strip()
    assert "job interview voice agent" in prompt.lower()


def test_build_runtime_config_uses_defaults() -> None:
    config = build_runtime_config({})

    assert config.agent_name == AGENT_NAME
    assert config.openrouter_model == DEFAULT_OPENROUTER_MODEL
    assert config.deepgram_stt_language == DEFAULT_DEEPGRAM_STT_LANGUAGE
    assert config.deepgram_stt_model == DEFAULT_DEEPGRAM_STT_MODEL
    assert config.sarvam_tts_language == DEFAULT_SARVAM_TTS_LANGUAGE
    assert config.sarvam_tts_model == DEFAULT_SARVAM_TTS_MODEL
    assert config.sarvam_tts_speaker == DEFAULT_SARVAM_TTS_SPEAKER


def test_build_runtime_config_honors_overrides() -> None:
    config = build_runtime_config(
        {
            "AGENT_NAME": "custom-job-agent",
            "OPENROUTER_MODEL": "openai/gpt-5.1",
            "DEEPGRAM_STT_LANGUAGE": "en-US",
            "DEEPGRAM_STT_MODEL": "nova-2",
            "SARVAM_TTS_LANGUAGE": "hi-IN",
            "SARVAM_TTS_MODEL": "bulbul:v2",
            "SARVAM_TTS_SPEAKER": "anushka",
        }
    )

    assert config.agent_name == "custom-job-agent"
    assert config.openrouter_model == "openai/gpt-5.1"
    assert config.deepgram_stt_language == "en-US"
    assert config.deepgram_stt_model == "nova-2"
    assert config.sarvam_tts_language == "hi-IN"
    assert config.sarvam_tts_model == "bulbul:v2"
    assert config.sarvam_tts_speaker == "anushka"


def test_resolve_user_id_from_room_metadata_prefers_explicit_value() -> None:
    user_id = resolve_user_id_from_room_metadata('{"user_id":"user_+919999999999"}')

    assert user_id == "user_+919999999999"


def test_parse_room_metadata_returns_empty_dict_for_invalid_json() -> None:
    assert parse_room_metadata("not-json") == {}


def test_resolve_session_mode_defaults_to_practice() -> None:
    assert resolve_session_mode({}) is SessionMode.PRACTICE


def test_resolve_session_mode_reads_diagnostics_flag() -> None:
    metadata = parse_room_metadata('{"sessionMode":"diagnostics"}')

    assert resolve_session_mode(metadata) is SessionMode.DIAGNOSTICS


def test_build_agent_session_uses_manual_turn_detection_in_diagnostics() -> None:
    session = build_agent_session(build_runtime_config({}), SessionMode.DIAGNOSTICS)

    assert session.turn_detection == "manual"


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (
        os.getenv("RUN_LIVE_AGENT_TESTS") == "1"
        and os.getenv("OPENROUTER_API_KEY")
    ),
    reason="Set RUN_LIVE_AGENT_TESTS=1 and OPENROUTER_API_KEY to run live tests",
)
async def test_agent_greets_user_with_job_search_context() -> None:
    from livekit.agents import AgentSession
    from livekit.plugins import openai

    llm = openai.LLM.with_openrouter(model=DEFAULT_OPENROUTER_MODEL)

    async with llm, AgentSession(llm=llm) as session:
        await session.start(JobFinderAgent())
        result = await session.run(user_input="Hello")

        await (
            result.expect.next_event()
            .is_message(role="assistant")
            .judge(
                llm,
                intent=(
                    "Introduces itself as a job search agent and starts helping "
                    "the user clarify the role they want to land."
                ),
            )
        )

        result.expect.no_more_events()
