from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import (
    AGENT_NAME,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_SARVAM_TTS_DICT_ID,
    DEFAULT_SARVAM_TTS_LANGUAGE,
    DEFAULT_SARVAM_TTS_MODEL,
    DEFAULT_SARVAM_TTS_SPEAKER,
    PROMPT_PATH,
    InterviewCoachingAgent,
    SessionConfig,
    SessionMode,
    build_agent_session,
    build_runtime_config,
    extract_session_config,
    load_prompt,
    parse_room_metadata,
    resolve_session_mode,
)
from language import resolve_language_config


def test_load_prompt_reads_prompt_file() -> None:
    prompt = load_prompt()

    assert prompt == PROMPT_PATH.read_text(encoding="utf-8").strip()
    assert "interview practice voice agent" in prompt.lower()


def test_build_runtime_config_uses_defaults() -> None:
    config = build_runtime_config({})

    assert config.agent_name == AGENT_NAME
    assert config.openrouter_model == DEFAULT_OPENROUTER_MODEL
    assert config.sarvam_tts_language == DEFAULT_SARVAM_TTS_LANGUAGE
    assert config.sarvam_tts_model == DEFAULT_SARVAM_TTS_MODEL
    assert config.sarvam_tts_speaker == DEFAULT_SARVAM_TTS_SPEAKER
    assert config.sarvam_tts_dict_id == DEFAULT_SARVAM_TTS_DICT_ID


def test_build_runtime_config_honors_overrides() -> None:
    config = build_runtime_config(
        {
            "AGENT_NAME": "custom-interview-agent",
            "OPENROUTER_MODEL": "openai/gpt-5.1",
            "SARVAM_TTS_LANGUAGE": "hi-IN",
            "SARVAM_TTS_MODEL": "bulbul:v3",
            "SARVAM_TTS_SPEAKER": "anushka",
            "SARVAM_TTS_DICT_ID": "p_test_dict",
        }
    )

    assert config.agent_name == "custom-interview-agent"
    assert config.openrouter_model == "openai/gpt-5.1"
    assert config.sarvam_tts_language == "hi-IN"
    assert config.sarvam_tts_model == "bulbul:v3"
    assert config.sarvam_tts_speaker == "anushka"
    assert config.sarvam_tts_dict_id == "p_test_dict"


def test_extract_session_config_reads_voice_and_speed() -> None:
    config = extract_session_config(
        {"sessionConfig": {"voice": "rahul", "speakingSpeed": "0.8"}}
    )

    assert config.voice == "rahul"
    assert config.speaking_speed == 0.8


def test_parse_room_metadata_returns_empty_dict_for_invalid_json() -> None:
    assert parse_room_metadata("not-json") == {}


def test_resolve_session_mode_defaults_to_practice() -> None:
    assert resolve_session_mode({}) is SessionMode.PRACTICE


def test_resolve_session_mode_reads_diagnostics_flag() -> None:
    metadata = parse_room_metadata('{"session_mode":"diagnostics"}')

    assert resolve_session_mode(metadata) is SessionMode.DIAGNOSTICS


def test_build_agent_session_uses_manual_turn_detection_in_diagnostics() -> None:
    with (
        patch("agent.sarvam.STT") as stt_mock,
        patch("agent.sarvam.TTS") as tts_mock,
        patch("agent.openai.LLM.with_openrouter") as llm_mock,
    ):
        stt_mock.return_value = MagicMock()
        tts_mock.return_value = MagicMock()
        llm_mock.return_value = MagicMock()
        session = build_agent_session(build_runtime_config({}), SessionMode.DIAGNOSTICS)

    assert session.turn_detection == "manual"


def test_build_agent_session_uses_sarvam_language_and_tts_options() -> None:
    with (
        patch("agent.sarvam.STT") as stt_mock,
        patch("agent.sarvam.TTS") as tts_mock,
        patch("agent.openai.LLM.with_openrouter") as llm_mock,
    ):
        stt_mock.return_value = MagicMock()
        tts_mock.return_value = MagicMock()
        llm_mock.return_value = MagicMock()

        build_agent_session(
            build_runtime_config({}),
            SessionMode.DIAGNOSTICS,
            SessionConfig(voice="rahul", speaking_speed=0.7),
            resolve_language_config("hindi"),
        )

    stt_kwargs = stt_mock.call_args.kwargs
    assert stt_kwargs["language"] == "hi-IN"
    assert stt_kwargs["mode"] == "codemix"

    tts_kwargs = tts_mock.call_args.kwargs
    assert tts_kwargs["target_language_code"] == "hi-IN"
    assert tts_kwargs["speaker"] == "rahul"
    assert tts_kwargs["pace"] == 0.7
    assert tts_kwargs["dict_id"] == DEFAULT_SARVAM_TTS_DICT_ID


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (
        os.getenv("RUN_LIVE_AGENT_TESTS") == "1"
        and os.getenv("OPENROUTER_API_KEY")
    ),
    reason="Set RUN_LIVE_AGENT_TESTS=1 and OPENROUTER_API_KEY to run live tests",
)
async def test_agent_greets_user_with_interview_context() -> None:
    from livekit.agents import AgentSession
    from livekit.plugins import openai

    llm = openai.LLM.with_openrouter(model=DEFAULT_OPENROUTER_MODEL)

    async with llm, AgentSession(llm=llm) as session:
        await session.start(InterviewCoachingAgent())
        result = await session.run(user_input="Hi")

        await (
            result.expect.next_event()
            .is_message(role="assistant")
            .judge(
                llm,
                intent=(
                    "Introduces itself as an interview coach and starts helping "
                    "the user prepare for interviews in a concise spoken style."
                ),
            )
        )

        result.expect.no_more_events()
