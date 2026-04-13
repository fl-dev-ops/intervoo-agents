from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from livekit import rtc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent import (
    InteractionMode,
    RecordingSessionState,
    SessionConfig,
    _build_room_options,
    _recording_sessions,
    _register_push_to_talk_rpcs,
    _start_ptt_session,
    build_end_call_tool,
    build_agent_session,
    build_runtime_config,
    extract_session_config,
    on_session_end,
    parse_room_metadata,
    resolve_interaction_mode,
    VoiceAssistantAgent,
)
from constants import (
    DEFAULT_DEEPGRAM_STT_LANGUAGE,
    DEFAULT_DEEPGRAM_STT_MODEL,
    DEFAULT_PROMPT_AGENT_NAME,
    DEFAULT_PROMPT_USER_NAME,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_SARVAM_TTS_LANGUAGE,
    DEFAULT_SARVAM_TTS_MODEL,
    DEFAULT_SARVAM_TTS_SPEAKER,
    PROMPT_PATH,
    REGISTERED_AGENT_NAME,
)
from prompt import build_prompt_context, load_prompt, render_prompt
from recording import RecordingConfig
from watchdog import (
    _idle_room_watchdogs,
    cancel_idle_room_watchdog,
    is_user_participant,
    register_idle_room_watchdog,
    room_has_user_participants,
    sync_idle_room_watchdog,
)


class FakeRoom:
    def __init__(
        self, name: str, participants: list[SimpleNamespace] | None = None
    ) -> None:
        self.name = name
        self.remote_participants = {
            participant.identity: participant for participant in (participants or [])
        }
        self._callbacks: dict[str, list] = {}
        self.local_participant = SimpleNamespace(
            register_rpc_method=self.register_rpc_method
        )
        self._rpc_handlers: dict[str, object] = {}

    def on(self, event: str, callback=None):
        def _register(fn):
            self._callbacks.setdefault(event, []).append(fn)
            return fn

        if callback is not None:
            return _register(callback)
        return _register

    def emit(self, event: str, participant: SimpleNamespace) -> None:
        callbacks = list(self._callbacks.get(event, []))
        for callback in callbacks:
            callback(participant)

    def register_rpc_method(self, method: str, callback=None):
        def _register(fn):
            self._rpc_handlers[method] = fn
            return fn

        if callback is not None:
            return _register(callback)
        return _register


class FakeJobContext:
    def __init__(self, room: FakeRoom) -> None:
        self.room = room
        self.api = SimpleNamespace()
        self.deleted_rooms: list[str] = []

    async def delete_room(self, room_name: str):
        self.deleted_rooms.append(room_name)
        return SimpleNamespace()


def _participant(identity: str, kind: rtc.ParticipantKind.ValueType) -> SimpleNamespace:
    return SimpleNamespace(identity=identity, kind=kind)


def test_load_prompt_reads_prompt_file() -> None:
    prompt = load_prompt()

    assert prompt == PROMPT_PATH.read_text(encoding="utf-8").strip()
    assert "ai voice agent for intervoo" in prompt.lower()


def test_build_runtime_config_uses_defaults() -> None:
    config = build_runtime_config({})

    assert config.agent_name == REGISTERED_AGENT_NAME
    assert config.openrouter_model == DEFAULT_OPENROUTER_MODEL
    assert config.deepgram_stt_language == DEFAULT_DEEPGRAM_STT_LANGUAGE
    assert config.deepgram_stt_model == DEFAULT_DEEPGRAM_STT_MODEL
    assert config.sarvam_tts_language == DEFAULT_SARVAM_TTS_LANGUAGE
    assert config.sarvam_tts_model == DEFAULT_SARVAM_TTS_MODEL
    assert config.sarvam_tts_speaker == DEFAULT_SARVAM_TTS_SPEAKER


def test_build_runtime_config_honors_overrides() -> None:
    config = build_runtime_config(
        {
            "AGENT_NAME": "custom-interview-agent",
            "OPENROUTER_MODEL": "openai/gpt-5.1",
            "DEEPGRAM_STT_LANGUAGE": "en-US",
            "DEEPGRAM_STT_MODEL": "nova-2",
            "SARVAM_TTS_LANGUAGE": "hi-IN",
            "SARVAM_TTS_MODEL": "bulbul:v2",
            "SARVAM_TTS_SPEAKER": "anushka",
        }
    )

    assert config.agent_name == "custom-interview-agent"
    assert config.openrouter_model == "openai/gpt-5.1"
    assert config.deepgram_stt_language == "en-US"
    assert config.deepgram_stt_model == "nova-2"
    assert config.sarvam_tts_language == "hi-IN"
    assert config.sarvam_tts_model == "bulbul:v2"
    assert config.sarvam_tts_speaker == "anushka"


def test_parse_room_metadata_returns_empty_dict_for_invalid_json() -> None:
    assert parse_room_metadata("not-json") == {}


def test_resolve_interaction_mode_defaults_to_auto() -> None:
    assert resolve_interaction_mode({}) is InteractionMode.AUTO


def test_resolve_interaction_mode_reads_ptt_flag() -> None:
    metadata = parse_room_metadata('{"interaction_mode":"ptt"}')

    assert resolve_interaction_mode(metadata) is InteractionMode.PTT


def test_resolve_interaction_mode_reads_auto_flag() -> None:
    metadata = parse_room_metadata('{"interaction_mode":"auto"}')

    assert resolve_interaction_mode(metadata) is InteractionMode.AUTO


def test_build_prompt_context_uses_defaults() -> None:
    context = build_prompt_context({})

    assert context == {
        "agentName": DEFAULT_PROMPT_AGENT_NAME,
        "additionalContext": "",
        "userName": DEFAULT_PROMPT_USER_NAME,
    }


def test_build_prompt_context_merges_prompt_context_values() -> None:
    context = build_prompt_context(
        {
            "userName": "Top Level",
            "prompt_context": {
                "agentName": "Maya",
                "userName": "Asha",
                "jobRole": "Backend Developer",
                "nativeLanguage": "Tamil",
            },
        }
    )

    assert context == {
        "agentName": "Maya",
        "additionalContext": '{"jobRole": "Backend Developer", "nativeLanguage": "Tamil"}',
        "userName": "Asha",
        "jobRole": "Backend Developer",
        "nativeLanguage": "Tamil",
    }


def test_extract_session_config_reads_config_values() -> None:
    session_config = extract_session_config(
        {"config": {"voice": "ishita", "speakingSpeed": "0.7"}}
    )

    assert session_config == SessionConfig(voice="ishita", speaking_speed=0.7)


def test_extract_session_config_ignores_invalid_values() -> None:
    session_config = extract_session_config(
        {"config": {"voice": " ", "speakingSpeed": "fast"}}
    )

    assert session_config == SessionConfig()


def test_render_prompt_injects_context_and_blanks_missing_values() -> None:
    rendered = render_prompt(
        "Hello {agentName}. User: {userName}. Role: {jobRole}. Missing: {companyName}",
        context={"agentName": "Sara", "userName": "Ravi", "jobRole": "Analyst"},
    )

    assert rendered == "Hello Sara. User: Ravi. Role: Analyst. Missing:"


def test_build_agent_session_uses_manual_turn_detection_in_ptt() -> None:
    session = build_agent_session(build_runtime_config({}), InteractionMode.PTT)

    assert session.turn_detection == "manual"


def test_build_agent_session_uses_tts_overrides_from_session_config() -> None:
    with patch("agent.sarvam.TTS") as tts_mock:
        tts_instance = MagicMock()
        tts_mock.return_value = tts_instance

        build_agent_session(
            build_runtime_config({}),
            InteractionMode.PTT,
            SessionConfig(voice="rahul", speaking_speed=0.7),
        )

    tts_mock.assert_called_once()
    tts_kwargs = tts_mock.call_args.kwargs
    assert tts_kwargs["speaker"] == "rahul"
    assert tts_kwargs["pace"] == 0.7


def test_voice_assistant_agent_exposes_end_call_tool() -> None:
    agent = VoiceAssistantAgent()

    assert [tool.id for tool in agent.tools] == ["end_call"]


def test_build_end_call_tool_returns_goodbye_instructions() -> None:
    end_call_tool = build_end_call_tool()

    assert end_call_tool.tools[0].info.name == "end_call"
    assert len(end_call_tool.tools) == 1


def test_build_room_options_keeps_text_input_enabled() -> None:
    options = _build_room_options()

    assert options.get_text_input_options() is not None


@pytest.mark.asyncio
async def test_register_push_to_talk_rpcs_links_participant_via_session_room_io() -> (
    None
):
    room = FakeRoom("rpc-room")
    ctx = FakeJobContext(room)
    room_io = SimpleNamespace(set_participant=MagicMock())
    session = SimpleNamespace(
        interrupt=MagicMock(),
        clear_user_turn=MagicMock(),
        input=SimpleNamespace(set_audio_enabled=MagicMock()),
        room_io=room_io,
        commit_user_turn=MagicMock(),
    )

    _register_push_to_talk_rpcs(ctx, session)
    start_turn = room._rpc_handlers["start_turn"]

    await start_turn(SimpleNamespace(caller_identity="web-user"))

    session.interrupt.assert_called_once()
    session.clear_user_turn.assert_called_once()
    room_io.set_participant.assert_called_once_with("web-user")
    session.input.set_audio_enabled.assert_called_once_with(True)


@pytest.mark.asyncio
async def test_start_ptt_session_uses_standard_session_start() -> None:
    room = FakeRoom("diagnostics-room")
    ctx = FakeJobContext(room)
    agent = VoiceAssistantAgent(instructions="Test instructions")
    session = SimpleNamespace(
        start=AsyncMock(),
        input=SimpleNamespace(set_audio_enabled=MagicMock()),
        generate_reply=AsyncMock(),
        room_io=SimpleNamespace(set_participant=MagicMock()),
    )
    config = build_runtime_config({})

    await _start_ptt_session(ctx, session, config, agent)

    session.start.assert_awaited_once()
    call_kwargs = session.start.await_args.kwargs
    assert call_kwargs["room"] is room
    assert call_kwargs["agent"] is agent
    assert call_kwargs["room_options"].get_text_input_options() is not None
    session.input.set_audio_enabled.assert_called_once_with(False)
    session.generate_reply.assert_awaited_once_with(instructions=config.initial_reply)
    assert set(room._rpc_handlers) >= {
        "start_turn",
        "end_turn",
        "cancel_turn",
        "pause_session",
    }


def test_is_user_participant_accepts_standard_and_sip() -> None:
    assert is_user_participant(
        _participant("standard-user", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)
    )
    assert is_user_participant(
        _participant("sip-user", rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
    )
    assert not is_user_participant(
        _participant("agent-user", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)
    )


def test_room_has_user_participants_ignores_agent_only_presence() -> None:
    room = FakeRoom(
        "test-room",
        participants=[
            _participant("agent-user", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)
        ],
    )

    assert not room_has_user_participants(room)


@pytest.mark.asyncio
async def test_idle_room_watchdog_deletes_room_after_timeout_without_users() -> None:
    room = FakeRoom("idle-room")
    ctx = FakeJobContext(room)

    sync_idle_room_watchdog(ctx, timeout_seconds=0)
    await asyncio.sleep(0.01)

    assert ctx.deleted_rooms == ["idle-room"]
    assert "idle-room" not in _idle_room_watchdogs


@pytest.mark.asyncio
async def test_idle_room_watchdog_cancels_when_standard_user_joins() -> None:
    room = FakeRoom("join-room")
    ctx = FakeJobContext(room)
    register_idle_room_watchdog(ctx, timeout_seconds=1)

    user = _participant("candidate", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD)
    room.remote_participants[user.identity] = user
    room.emit("participant_connected", user)
    await asyncio.sleep(0)

    assert "join-room" not in _idle_room_watchdogs
    assert ctx.deleted_rooms == []


@pytest.mark.asyncio
async def test_idle_room_watchdog_restarts_when_last_sip_user_leaves() -> None:
    user = _participant("caller", rtc.ParticipantKind.PARTICIPANT_KIND_SIP)
    room = FakeRoom("leave-room", participants=[user])
    ctx = FakeJobContext(room)
    register_idle_room_watchdog(ctx, timeout_seconds=0)

    room.remote_participants.pop(user.identity)
    room.emit("participant_disconnected", user)
    await asyncio.sleep(0.01)

    assert ctx.deleted_rooms == ["leave-room"]


@pytest.mark.asyncio
async def test_on_session_end_cancels_watchdog_and_keeps_recording_finalize_flow() -> (
    None
):
    room = FakeRoom("recording-room")
    ctx = FakeJobContext(room)
    report = SimpleNamespace(
        to_dict=lambda: {"room": room.name}, started_at=1.0, duration=2.0
    )
    ctx.make_session_report = lambda: report

    watchdog = asyncio.create_task(asyncio.sleep(60))
    _idle_room_watchdogs[room.name] = watchdog
    _recording_sessions[room.name] = RecordingSessionState(
        config=RecordingConfig(s3_bucket="bucket"),
        egress_id="egress-1",
        room_name=room.name,
        audio_url="https://example.com/audio.mp3",
        audio_s3_key="agents/audio.mp3",
        resolved_user_id="user_1",
        participant_identity="participant_1",
        phone_number="+911234567890",
    )

    with patch("agent.finalize_recording", new_callable=AsyncMock) as finalize_mock:
        await on_session_end(ctx)
    await asyncio.sleep(0)

    finalize_mock.assert_awaited_once()
    assert room.name not in _idle_room_watchdogs
    assert watchdog.cancelled()


@pytest.fixture(autouse=True)
def clear_agent_state() -> None:
    yield
    for room_name in list(_idle_room_watchdogs):
        cancel_idle_room_watchdog(room_name)
    _recording_sessions.clear()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not (os.getenv("RUN_LIVE_AGENT_TESTS") == "1" and os.getenv("OPENROUTER_API_KEY")),
    reason="Set RUN_LIVE_AGENT_TESTS=1 and OPENROUTER_API_KEY to run live tests",
)
async def test_agent_greets_user_with_interview_context() -> None:
    from livekit.agents import AgentSession
    from livekit.plugins import openai

    llm = openai.LLM.with_openrouter(model=DEFAULT_OPENROUTER_MODEL)

    async with llm, AgentSession(llm=llm) as session:
        await session.start(VoiceAssistantAgent())
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
