from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    MetricsCollectedEvent,
    RoomIO,
    metrics,
    room_io,
)
from livekit.plugins import noise_cancellation, openai, sarvam, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from constants import (
    AGENT_NAME,
    CALLER_LOOKUP_TIMEOUT_SECONDS,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_PROMPT_AGENT_NAME,
    DEFAULT_PROMPT_USER_NAME,
    DEFAULT_SARVAM_TTS_DICT_ID,
    DEFAULT_SARVAM_TTS_LANGUAGE,
    DEFAULT_SARVAM_TTS_MODEL,
    DEFAULT_SARVAM_TTS_SPEAKER,
    INITIAL_REPLY,
    PROMPT_PATH,
    REGISTERED_AGENT_NAME,
)
from identity import (
    resolve_phone_number_from_call_context,
    resolve_user_id_from_call_context,
    resolve_user_id_from_room_metadata,
)
from language import resolve_language_config, resolve_stt_mode
from prompt import build_prompt_context, load_prompt, render_prompt
from recording_config import RecordingConfig, build_recording_config
from recording_db import init_pool
from recording_runtime import finalize_recording, start_recording
from tracing import flush_langfuse, setup_langfuse
from watchdog import cancel_idle_room_watchdog, register_idle_room_watchdog

logger = logging.getLogger("interview_coaching_agent")
MAX_CONCURRENT_SESSIONS = 10

__all__ = [
    "AGENT_NAME",
    "DEFAULT_OPENROUTER_MODEL",
    "DEFAULT_PROMPT_AGENT_NAME",
    "DEFAULT_PROMPT_USER_NAME",
    "DEFAULT_SARVAM_TTS_DICT_ID",
    "DEFAULT_SARVAM_TTS_LANGUAGE",
    "DEFAULT_SARVAM_TTS_MODEL",
    "DEFAULT_SARVAM_TTS_SPEAKER",
    "PROMPT_PATH",
    "InterviewCoachingAgent",
    "RuntimeConfig",
    "SessionConfig",
    "SessionMode",
    "build_agent_session",
    "build_runtime_config",
    "extract_session_config",
    "load_prompt",
    "parse_room_metadata",
    "resolve_session_mode",
]


class SessionMode(str, Enum):
    PRACTICE = "practice"
    DIAGNOSTICS = "diagnostics"


@dataclass(frozen=True)
class RuntimeConfig:
    agent_name: str = REGISTERED_AGENT_NAME
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL
    sarvam_tts_language: str = DEFAULT_SARVAM_TTS_LANGUAGE
    sarvam_tts_model: str = DEFAULT_SARVAM_TTS_MODEL
    sarvam_tts_speaker: str = DEFAULT_SARVAM_TTS_SPEAKER
    sarvam_tts_dict_id: str | None = DEFAULT_SARVAM_TTS_DICT_ID
    initial_reply: str = INITIAL_REPLY


@dataclass(frozen=True)
class RecordingSessionState:
    config: RecordingConfig
    session_id: str | None
    egress_id: str | None
    room_name: str
    resolved_user_id: str | None
    participant_identity: str | None
    phone_number: str | None


_recording_sessions: dict[str, RecordingSessionState] = {}


@dataclass(frozen=True)
class SessionConfig:
    voice: str | None = None
    speaking_speed: float | None = None


def build_runtime_config(env: Mapping[str, str] | None = None) -> RuntimeConfig:
    values = os.environ if env is None else env
    return RuntimeConfig(
        agent_name=values.get("AGENT_NAME", REGISTERED_AGENT_NAME),
        openrouter_model=values.get("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL),
        sarvam_tts_language=values.get(
            "SARVAM_TTS_LANGUAGE", DEFAULT_SARVAM_TTS_LANGUAGE
        ),
        sarvam_tts_model=values.get("SARVAM_TTS_MODEL", DEFAULT_SARVAM_TTS_MODEL),
        sarvam_tts_speaker=values.get("SARVAM_TTS_SPEAKER", DEFAULT_SARVAM_TTS_SPEAKER),
        sarvam_tts_dict_id=(
            values.get("SARVAM_TTS_DICT_ID", DEFAULT_SARVAM_TTS_DICT_ID).strip() or None
        ),
        initial_reply=INITIAL_REPLY,
    )


def parse_room_metadata(metadata: str | None) -> dict[str, object]:
    if not metadata:
        return {}

    try:
        payload = json.loads(metadata)
    except json.JSONDecodeError:
        logger.warning("Room metadata is not valid JSON")
        return {}

    if isinstance(payload, dict):
        return payload

    logger.warning("Room metadata is not an object")
    return {}


def extract_session_config(metadata: Mapping[str, object]) -> SessionConfig:
    raw_config = metadata.get("session_config") or metadata.get("sessionConfig")
    if not isinstance(raw_config, Mapping):
        return SessionConfig()

    voice = raw_config.get("voice")
    speaking_speed = raw_config.get("speaking_speed") or raw_config.get(
        "speakingSpeed"
    )

    resolved_speed: float | None = None
    if isinstance(speaking_speed, int | float):
        resolved_speed = float(speaking_speed)
    elif isinstance(speaking_speed, str):
        try:
            resolved_speed = float(speaking_speed)
        except ValueError:
            logger.warning("Invalid speaking speed in session config")

    return SessionConfig(
        voice=voice.strip() if isinstance(voice, str) and voice.strip() else None,
        speaking_speed=resolved_speed,
    )


def resolve_session_mode(metadata: Mapping[str, object] | None) -> SessionMode:
    if not metadata:
        return SessionMode.PRACTICE

    candidates = (
        metadata.get("session_mode"),
        metadata.get("sessionMode"),
        metadata.get("mode"),
    )
    for candidate in candidates:
        if isinstance(candidate, str):
            normalized = candidate.strip().lower()
            if normalized == SessionMode.DIAGNOSTICS.value:
                return SessionMode.DIAGNOSTICS
            if normalized == SessionMode.PRACTICE.value:
                return SessionMode.PRACTICE

    return SessionMode.PRACTICE


def build_recording_metadata(
    room_metadata: Mapping[str, object] | None,
    mode: SessionMode,
) -> dict[str, object]:
    metadata = dict(room_metadata) if room_metadata else {}
    metadata["session_mode"] = mode.value
    return metadata


class InterviewCoachingAgent(Agent):
    def __init__(self, instructions: str | None = None) -> None:
        super().__init__(instructions=instructions or render_prompt())


def build_agent_session(
    config: RuntimeConfig,
    mode: SessionMode = SessionMode.PRACTICE,
    session_config: SessionConfig | None = None,
    language_info: Mapping[str, str] | None = None,
) -> AgentSession:
    effective_session_config = session_config or SessionConfig()
    effective_language = language_info or resolve_language_config(None)
    stt_language = effective_language.get("stt_language", "en-IN")
    tts_language = effective_language.get("tts_language", config.sarvam_tts_language)

    common_kwargs = {
        "stt": sarvam.STT(
            language=stt_language,
            model="saaras:v3",
            mode=resolve_stt_mode(stt_language),
        ),
        "llm": openai.LLM.with_openrouter(model=config.openrouter_model),
        "tts": sarvam.TTS(
            target_language_code=tts_language,
            model=config.sarvam_tts_model,
            speaker=effective_session_config.voice or config.sarvam_tts_speaker,
            pace=effective_session_config.speaking_speed or 1.0,
            temperature=0.6,
            enable_preprocessing=True,
            output_audio_bitrate="128k",
            min_buffer_size=50,
            max_chunk_length=150,
            dict_id=config.sarvam_tts_dict_id,
        ),
        "allow_interruptions": True,
        "min_interruption_duration": 0.5,
        "min_endpointing_delay": 0.5,
        "max_endpointing_delay": 3.0,
        "min_consecutive_speech_delay": 0.2,
    }

    if mode is SessionMode.DIAGNOSTICS:
        return AgentSession(
            **common_kwargs,
            turn_detection="manual",
            resume_false_interruption=True,
            use_tts_aligned_transcript=True,
            preemptive_generation=True,
        )

    return AgentSession(
        **common_kwargs,
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )


def _attach_metrics_logging(session: AgentSession) -> None:
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)


def _build_room_options() -> room_io.RoomOptions:
    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(
            noise_cancellation=lambda params: (
                noise_cancellation.BVCTelephony()
                if params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
                else noise_cancellation.BVC()
            ),
        ),
        close_on_disconnect=False,
    )


def _register_push_to_talk_rpcs(
    ctx: agents.JobContext,
    session: AgentSession,
    diagnostic_room_io: RoomIO | None = None,
) -> None:
    @ctx.room.local_participant.register_rpc_method("start_turn")
    async def start_turn(data: rtc.RpcInvocationData) -> str:
        logger.info(f"start_turn RPC called by {data.caller_identity}")
        session.interrupt()
        session.clear_user_turn()
        if diagnostic_room_io is not None:
            diagnostic_room_io.set_participant(data.caller_identity)
        elif getattr(session, "room_io", None) is not None:
            session.room_io.set_participant(data.caller_identity)
        session.input.set_audio_enabled(True)
        return "ok"

    @ctx.room.local_participant.register_rpc_method("end_turn")
    async def end_turn(data: rtc.RpcInvocationData) -> str:
        logger.info(f"end_turn RPC called by {data.caller_identity}")
        session.input.set_audio_enabled(False)
        session.commit_user_turn(
            transcript_timeout=3.0,
            stt_flush_duration=0.5,
        )
        return "ok"

    @ctx.room.local_participant.register_rpc_method("cancel_turn")
    async def cancel_turn(data: rtc.RpcInvocationData) -> str:
        logger.info(f"cancel_turn RPC called by {data.caller_identity}")
        session.input.set_audio_enabled(False)
        session.clear_user_turn()
        return "ok"

    @ctx.room.local_participant.register_rpc_method("pause_session")
    async def pause_session(data: rtc.RpcInvocationData) -> str:
        logger.info(f"pause_session RPC called by {data.caller_identity}")
        session.interrupt()
        session.input.set_audio_enabled(False)
        return "ok"


async def _start_practice_session(
    ctx: agents.JobContext,
    session: AgentSession,
    config: RuntimeConfig,
    agent: InterviewCoachingAgent,
) -> None:
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=_build_room_options(),
    )
    logger.info("Interview coaching practice session started")
    await session.generate_reply(instructions=config.initial_reply)


async def _start_diagnostics_session(
    ctx: agents.JobContext,
    session: AgentSession,
    config: RuntimeConfig,
    agent: InterviewCoachingAgent,
) -> None:
    diagnostic_room_io = RoomIO(
        session,
        room=ctx.room,
        options=_build_room_options(),
    )
    await diagnostic_room_io.start()
    logger.info("Interview coaching diagnostics RoomIO started")

    await session.start(agent=agent)

    session.input.set_audio_enabled(False)
    _register_push_to_talk_rpcs(ctx, session, diagnostic_room_io)
    logger.info("Interview coaching diagnostics session started")
    await session.generate_reply(instructions=config.initial_reply)


def _pick_call_participant(ctx: agents.JobContext) -> rtc.RemoteParticipant | None:
    participants = list(ctx.room.remote_participants.values())
    for participant in participants:
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return participant
    return participants[0] if participants else None


async def _resolve_call_state(
    ctx: agents.JobContext,
    initial_user_id: str,
) -> tuple[str, str | None, str | None]:
    participant = _pick_call_participant(ctx)
    if participant is None:
        try:
            participant = await asyncio.wait_for(
                ctx.wait_for_participant(),
                timeout=CALLER_LOOKUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            participant = None

    participant_identity = participant.identity if participant else None
    participant_attributes = (
        dict(participant.attributes.items())
        if participant and participant.attributes
        else None
    )
    resolved_user_id = resolve_user_id_from_call_context(
        current_user_id=initial_user_id,
        participant_identity=participant_identity,
        participant_attributes=participant_attributes,
        room_name=ctx.room.name,
    )
    phone_number = resolve_phone_number_from_call_context(
        participant_identity=participant_identity,
        participant_attributes=participant_attributes,
        room_name=ctx.room.name,
    )
    return resolved_user_id, participant_identity, phone_number


def _compute_worker_load(current_server: AgentServer) -> float:
    return 1.0 if len(current_server.active_jobs) >= MAX_CONCURRENT_SESSIONS else 0.0


server = AgentServer(
    shutdown_process_timeout=60,
    load_fnc=_compute_worker_load,
    load_threshold=0.5,
)


async def on_session_end(ctx: agents.JobContext) -> None:
    cancel_idle_room_watchdog(ctx.room.name)

    state = _recording_sessions.pop(ctx.room.name, None)
    if state is None or not state.config.enabled:
        flush_langfuse()
        return

    report_dict: dict = {}
    try:
        report = ctx.make_session_report()
        report_dict = report.to_dict()
        report_dict.setdefault("started_at", report.started_at)
        report_dict.setdefault("duration", report.duration)
    except Exception as e:
        logger.warning(f"Failed to create session report: {e}")

    try:
        await finalize_recording(
            config=state.config,
            lk_api=ctx.api,
            session_id=state.session_id,
            egress_id=state.egress_id,
            agent_type="interview-agent",
            agent_name=REGISTERED_AGENT_NAME,
            room_name=state.room_name,
            report_dict=report_dict,
            resolved_user_id=state.resolved_user_id,
            participant_identity=state.participant_identity,
            phone_number=state.phone_number,
        )
    except Exception as e:
        logger.error(f"Recording finalization failed: {e}")

    flush_langfuse()


@server.rtc_session(agent_name=REGISTERED_AGENT_NAME, on_session_end=on_session_end)
async def entrypoint(ctx: agents.JobContext) -> None:
    config = build_runtime_config()
    room_metadata = ctx.job.room.metadata or ctx.room.metadata
    metadata = parse_room_metadata(room_metadata)
    mode = resolve_session_mode(metadata)
    session_config = extract_session_config(metadata)
    recording_metadata = build_recording_metadata(metadata, mode)
    await ctx.connect()
    register_idle_room_watchdog(ctx)

    logger.info(f"Room metadata: {json.dumps(metadata, indent=2)}")
    student_profile = metadata.get("studentProfile")
    if student_profile:
        logger.info(f"Student profile: {json.dumps(student_profile, indent=2)}")
    else:
        logger.warning("No student profile found in room metadata")

    initial_user_id = resolve_user_id_from_room_metadata(room_metadata)
    resolved_user_id, participant_identity, phone_number = await _resolve_call_state(
        ctx, initial_user_id
    )

    try:
        setup_langfuse(
            metadata={
                "langfuse.session.id": ctx.room.name,
                "langfuse.user.id": resolved_user_id or "anonymous",
                "agent_name": REGISTERED_AGENT_NAME,
                "job_id": ctx.job.id,
                "mode": mode.value,
            }
        )
    except Exception as e:
        logger.warning(f"Langfuse setup failed: {e}")

    prompt_context = build_prompt_context(metadata)
    agent_instructions = render_prompt(context=prompt_context)

    comfortable_language: str | None = None
    raw_prompt_context = metadata.get("prompt_context") or metadata.get("promptContext")
    if isinstance(raw_prompt_context, Mapping):
        candidate = raw_prompt_context.get("comfortableLanguage")
        if isinstance(candidate, str) and candidate.strip():
            comfortable_language = candidate.strip()
    language_info = resolve_language_config(comfortable_language)

    agent = InterviewCoachingAgent(instructions=agent_instructions)

    session = build_agent_session(config, mode, session_config, language_info)
    _attach_metrics_logging(session)

    rec_cfg = build_recording_config()
    if rec_cfg.enabled:
        try:
            await init_pool(rec_cfg.database_url)
            room_sid = await ctx.room.sid
            session_id, egress_id = await start_recording(
                config=rec_cfg,
                lk_api=ctx.api,
                agent_type="interview-agent",
                agent_name=config.agent_name,
                room_name=ctx.room.name,
                room_sid=room_sid,
                resolved_user_id=resolved_user_id,
                participant_identity=participant_identity,
                phone_number=phone_number,
                metadata=recording_metadata,
            )
            _recording_sessions[ctx.room.name] = RecordingSessionState(
                config=rec_cfg,
                session_id=session_id,
                egress_id=egress_id,
                room_name=ctx.room.name,
                resolved_user_id=resolved_user_id,
                participant_identity=participant_identity,
                phone_number=phone_number,
            )
        except Exception as e:
            logger.error(f"Failed to initialize recording: {e}")

    if mode is SessionMode.DIAGNOSTICS:
        await _start_diagnostics_session(ctx, session, config, agent)
    else:
        await _start_practice_session(ctx, session, config, agent)


if __name__ == "__main__":
    agents.cli.run_app(server)
