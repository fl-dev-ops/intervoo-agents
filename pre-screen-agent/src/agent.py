from __future__ import annotations

import asyncio
import inspect
import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from functools import wraps

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    MetricsCollectedEvent,
    llm,
    metrics,
    room_io,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import deepgram, google, noise_cancellation, openai, sarvam, silero
from livekit.plugins.sarvam import tts as sarvam_tts_module
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from language import (
    detect_language_style,
    resolve_language_config,
    resolve_stt_mode,
    response_style_instruction,
)

from constants import (
    CALLER_LOOKUP_TIMEOUT_SECONDS,
    DEFAULT_DEEPGRAM_STT_LANGUAGE,
    DEFAULT_DEEPGRAM_STT_MODEL,
    DEFAULT_OPENROUTER_MODEL,
    DEFAULT_SARVAM_TTS_DICT_ID,
    DEFAULT_SARVAM_TTS_LANGUAGE,
    DEFAULT_SARVAM_TTS_MODEL,
    DEFAULT_SARVAM_TTS_SPEAKER,
    END_CALL_EXTRA_DESCRIPTION,
    END_CALL_INSTRUCTIONS,
    INITIAL_REPLY,
    REGISTERED_AGENT_NAME,
)
from identity import (
    resolve_phone_number_from_call_context,
    resolve_user_id_from_call_context,
    resolve_user_id_from_room_metadata,
)
from prompt import build_prompt_context, render_prompt
from recording import (
    RecordingConfig,
    build_recording_config,
    finalize_recording,
    start_recording,
)
from tracing import flush_langfuse, setup_langfuse
from watchdog import cancel_idle_room_watchdog, register_idle_room_watchdog

logger = logging.getLogger("interview_coaching_agent")
MAX_CONCURRENT_SESSIONS = 10


def _patch_sarvam_tts_compatibility() -> None:
    if getattr(sarvam_tts_module, "_intervoo_compat_patch_applied", False):
        return

    def _patch_output_emitter_mime_type(output_emitter):
        original_initialize = output_emitter.initialize

        def _patched_initialize(*args, **kwargs):
            if kwargs.get("mime_type") == "audio/wav":
                kwargs = {**kwargs, "mime_type": "audio/mpeg"}
            return original_initialize(*args, **kwargs)

        output_emitter.initialize = _patched_initialize
        return original_initialize

    def _add_kwonly_signature(fn, original_fn, parameter_name: str):
        original_signature = inspect.signature(original_fn)
        parameters = list(original_signature.parameters.values())
        parameters.append(
            inspect.Parameter(
                parameter_name,
                kind=inspect.Parameter.KEYWORD_ONLY,
                default=None,
                annotation=str | None,
            )
        )
        fn.__signature__ = original_signature.replace(parameters=parameters)

    original_tts_init = sarvam_tts_module.TTS.__init__
    original_update_options = sarvam_tts_module.TTS.update_options
    original_chunked_init = sarvam_tts_module.ChunkedStream.__init__
    original_chunked_run = sarvam_tts_module.ChunkedStream._run
    original_synthesize_run = sarvam_tts_module.SynthesizeStream._run
    original_synthesize_init = sarvam_tts_module.SynthesizeStream.__init__
    original_run_ws = sarvam_tts_module.SynthesizeStream._run_ws

    @wraps(original_tts_init)
    def _patched_tts_init(self, *args, dict_id: str | None = None, **kwargs):
        original_tts_init(self, *args, **kwargs)
        self._dict_id = dict_id

    _add_kwonly_signature(_patched_tts_init, original_tts_init, "dict_id")

    @wraps(original_update_options)
    def _patched_update_options(self, *args, dict_id: str | None = None, **kwargs):
        original_update_options(self, *args, **kwargs)
        if dict_id is not None:
            self._dict_id = dict_id

    _add_kwonly_signature(_patched_update_options, original_update_options, "dict_id")

    @wraps(original_chunked_init)
    def _patched_chunked_init(self, *, tts, input_text, conn_options):
        original_chunked_init(
            self, tts=tts, input_text=input_text, conn_options=conn_options
        )
        self._dict_id = getattr(tts, "_dict_id", None)

    @wraps(original_chunked_run)
    async def _patched_chunked_run(self, output_emitter, *args, **kwargs):
        original_initialize = _patch_output_emitter_mime_type(output_emitter)
        dict_id = getattr(self, "_dict_id", None)
        try:
            if not dict_id or self._opts.model != "bulbul:v3":
                return await original_chunked_run(self, output_emitter, *args, **kwargs)

            original_ensure_session = self._tts._ensure_session
            base_session = original_ensure_session()

            class _SessionProxy:
                def __init__(self, session):
                    self._session = session

                def post(self, *post_args, **post_kwargs):
                    payload = post_kwargs.get("json")
                    if isinstance(payload, dict):
                        post_kwargs = {
                            **post_kwargs,
                            "json": {**payload, "dict_id": dict_id},
                        }
                    return self._session.post(*post_args, **post_kwargs)

                def __getattr__(self, name):
                    return getattr(self._session, name)

            self._tts._ensure_session = lambda: _SessionProxy(base_session)
            try:
                return await original_chunked_run(self, output_emitter, *args, **kwargs)
            finally:
                self._tts._ensure_session = original_ensure_session
        finally:
            output_emitter.initialize = original_initialize

    @wraps(original_synthesize_init)
    def _patched_synthesize_init(self, *, tts, conn_options):
        original_synthesize_init(self, tts=tts, conn_options=conn_options)
        self._dict_id = getattr(tts, "_dict_id", None)

    @wraps(original_synthesize_run)
    async def _patched_synthesize_run(self, output_emitter, *args, **kwargs):
        original_initialize = _patch_output_emitter_mime_type(output_emitter)
        try:
            return await original_synthesize_run(self, output_emitter, *args, **kwargs)
        finally:
            output_emitter.initialize = original_initialize

    @wraps(original_run_ws)
    async def _patched_run_ws(self, word_stream, output_emitter):
        dict_id = getattr(self, "_dict_id", None)
        if not dict_id or self._opts.model != "bulbul:v3":
            return await original_run_ws(self, word_stream, output_emitter)

        original_pool = self._tts._pool

        class _ConnectionContext:
            def __init__(self, inner_context):
                self._inner_context = inner_context
                self._ws = None

            async def __aenter__(self):
                ws = await self._inner_context.__aenter__()
                original_send_str = ws.send_str

                async def _patched_send_str(message, *send_args, **send_kwargs):
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        return await original_send_str(
                            message, *send_args, **send_kwargs
                        )

                    if payload.get("type") == "config" and isinstance(
                        payload.get("data"), dict
                    ):
                        payload = {
                            **payload,
                            "data": {**payload["data"], "dict_id": dict_id},
                        }
                        message = json.dumps(payload)

                    return await original_send_str(message, *send_args, **send_kwargs)

                ws.send_str = _patched_send_str
                self._ws = ws
                return ws

            async def __aexit__(self, exc_type, exc, tb):
                return await self._inner_context.__aexit__(exc_type, exc, tb)

        class _PoolProxy:
            def __init__(self, pool):
                self._pool = pool

            def connection(self, *pool_args, **pool_kwargs):
                return _ConnectionContext(self._pool.connection(*pool_args, **pool_kwargs))

            def __getattr__(self, name):
                return getattr(self._pool, name)

        self._tts._pool = _PoolProxy(original_pool)
        try:
            return await original_run_ws(self, word_stream, output_emitter)
        finally:
            self._tts._pool = original_pool

    sarvam_tts_module.TTS.__init__ = _patched_tts_init
    sarvam_tts_module.TTS.update_options = _patched_update_options
    sarvam_tts_module.ChunkedStream.__init__ = _patched_chunked_init
    sarvam_tts_module.ChunkedStream._run = _patched_chunked_run
    sarvam_tts_module.SynthesizeStream.__init__ = _patched_synthesize_init
    sarvam_tts_module.SynthesizeStream._run = _patched_synthesize_run
    sarvam_tts_module.SynthesizeStream._run_ws = _patched_run_ws
    sarvam_tts_module._intervoo_compat_patch_applied = True


_patch_sarvam_tts_compatibility()


class InteractionMode(str, Enum):
    AUTO = "auto"
    PTT = "ptt"


@dataclass(frozen=True)
class RuntimeConfig:
    agent_name: str = REGISTERED_AGENT_NAME
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL
    deepgram_stt_language: str = DEFAULT_DEEPGRAM_STT_LANGUAGE
    deepgram_stt_model: str = DEFAULT_DEEPGRAM_STT_MODEL
    sarvam_tts_language: str = DEFAULT_SARVAM_TTS_LANGUAGE
    sarvam_tts_model: str = DEFAULT_SARVAM_TTS_MODEL
    sarvam_tts_speaker: str = DEFAULT_SARVAM_TTS_SPEAKER
    initial_reply: str = INITIAL_REPLY


@dataclass(frozen=True)
class RecordingSessionState:
    config: RecordingConfig
    egress_id: str | None
    room_name: str
    audio_url: str
    audio_s3_key: str
    resolved_user_id: str | None
    participant_identity: str | None
    phone_number: str | None
    metrics_events: list[dict] = field(default_factory=list)
    usage_collector: metrics.UsageCollector | None = None


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
        deepgram_stt_language=values.get(
            "DEEPGRAM_STT_LANGUAGE", DEFAULT_DEEPGRAM_STT_LANGUAGE
        ),
        deepgram_stt_model=values.get("DEEPGRAM_STT_MODEL", DEFAULT_DEEPGRAM_STT_MODEL),
        sarvam_tts_language=values.get(
            "SARVAM_TTS_LANGUAGE", DEFAULT_SARVAM_TTS_LANGUAGE
        ),
        sarvam_tts_model=values.get("SARVAM_TTS_MODEL", DEFAULT_SARVAM_TTS_MODEL),
        sarvam_tts_speaker=values.get("SARVAM_TTS_SPEAKER", DEFAULT_SARVAM_TTS_SPEAKER),
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


def extract_session_config(metadata: Mapping[str, object] | None) -> SessionConfig:
    if not metadata:
        return SessionConfig()

    raw_config = metadata.get("config")
    if not isinstance(raw_config, Mapping):
        return SessionConfig()

    voice = raw_config.get("voice")
    normalized_voice = (
        voice.strip() if isinstance(voice, str) and voice.strip() else None
    )

    speaking_speed = raw_config.get("speakingSpeed")
    normalized_speaking_speed: float | None = None
    if isinstance(speaking_speed, (int, float)) and math.isfinite(speaking_speed):
        normalized_speaking_speed = float(speaking_speed)
    elif isinstance(speaking_speed, str):
        try:
            parsed_speaking_speed = float(speaking_speed)
        except ValueError:
            parsed_speaking_speed = None
        if parsed_speaking_speed is not None and math.isfinite(parsed_speaking_speed):
            normalized_speaking_speed = parsed_speaking_speed

    return SessionConfig(
        voice=normalized_voice,
        speaking_speed=normalized_speaking_speed,
    )


def resolve_interaction_mode(metadata: Mapping[str, object] | None) -> InteractionMode:
    if not metadata:
        return InteractionMode.AUTO

    interaction_mode = metadata.get("interaction_mode") or metadata.get(
        "interactionMode"
    )
    if isinstance(interaction_mode, str):
        normalized_interaction_mode = interaction_mode.strip().lower()
        if normalized_interaction_mode == "ptt":
            return InteractionMode.PTT
        if normalized_interaction_mode == "auto":
            return InteractionMode.AUTO

    return InteractionMode.AUTO


def resolve_session_mode(metadata: Mapping[str, object] | None) -> InteractionMode:
    return resolve_interaction_mode(metadata)


def build_recording_metadata(
    room_metadata: Mapping[str, object] | None,
    mode: InteractionMode,
) -> dict[str, object]:
    metadata = dict(room_metadata) if room_metadata else {}
    metadata["interaction_mode"] = mode.value
    return metadata


def build_end_call_tool() -> EndCallTool:
    return EndCallTool(
        extra_description=END_CALL_EXTRA_DESCRIPTION,
        delete_room=True,
        end_instructions=END_CALL_INSTRUCTIONS,
    )


class VoiceAssistantAgent(Agent):
    def __init__(
        self,
        instructions: str | None = None,
        language_style: str = "English",
    ) -> None:
        super().__init__(
            instructions=instructions or render_prompt(),
            tools=[build_end_call_tool()],
        )
        self._current_style = language_style
        self._english_switch_streak = 0

    async def on_user_turn_completed(
        self,
        turn_ctx: llm.ChatContext,
        new_message: llm.ChatMessage,
    ) -> None:
        text = new_message.text_content or ""
        detected_style = detect_language_style(text, fallback=self._current_style)
        normalized_text = text.lower()
        explicit_english_switch = any(
            phrase in normalized_text
            for phrase in (
                "in english",
                "speak english",
                "english please",
                "let's do english",
                "lets do english",
            )
        )

        if detected_style in {"Tanglish", "Hinglish"}:
            self._current_style = detected_style
            self._english_switch_streak = 0
        elif detected_style == "English" and self._current_style in {"Tanglish", "Hinglish"}:
            self._english_switch_streak += 1
            if explicit_english_switch or self._english_switch_streak >= 2:
                self._current_style = "English"
                self._english_switch_streak = 0
        else:
            self._current_style = detected_style
            self._english_switch_streak = 0

        turn_ctx.add_message(
            role="developer",
            content=response_style_instruction(self._current_style),
        )


def build_agent_session(
    config: RuntimeConfig,
    mode: InteractionMode = InteractionMode.AUTO,
    session_config: SessionConfig | None = None,
    language_info: Mapping[str, str] | None = None,
) -> AgentSession:
    effective_session_config = session_config or SessionConfig()
    effective_language = language_info or resolve_language_config(None)
    stt_language = effective_language.get("stt_language", "en-IN")
    tts_language = effective_language.get("tts_language", config.sarvam_tts_language)
    stt = sarvam.STT(
        language=stt_language,
        model="saaras:v3",
        mode=resolve_stt_mode(stt_language),
    )

    llm = openai.LLM.with_openrouter(model=config.openrouter_model)

    tts = sarvam.TTS(
        target_language_code=tts_language,
        model=config.sarvam_tts_model,
        speaker=effective_session_config.voice or config.sarvam_tts_speaker,
        pace=effective_session_config.speaking_speed or 1.0,
        temperature=0.6,
        output_audio_bitrate="128k",
        min_buffer_size=50,
        max_chunk_length=150,
        dict_id=DEFAULT_SARVAM_TTS_DICT_ID,
    )

    if hasattr(tts, "prewarm"):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            tts.prewarm()

    common_kwargs = {
        "stt": stt,
        "llm": llm,
        "tts": tts,
        "allow_interruptions": True,
        "min_interruption_duration": 0.5,
        "min_endpointing_delay": 0.5,
        "max_endpointing_delay": 3.0,
        "min_consecutive_speech_delay": 0.2,
    }

    if mode is InteractionMode.PTT:
        return AgentSession(
            **common_kwargs,
            turn_detection="manual",
            resume_false_interruption=True,
            use_tts_aligned_transcript=True,
            preemptive_generation=False,
        )

    return AgentSession(
        **common_kwargs,
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )


def _serialize_metric(metric_obj: object) -> dict:
    if hasattr(metric_obj, "model_dump"):
        try:
            return metric_obj.model_dump()
        except Exception:
            pass
    if hasattr(metric_obj, "__dict__"):
        return {k: v for k, v in vars(metric_obj).items() if not k.startswith("_")}
    return {"repr": repr(metric_obj)}


def _attach_metrics_logging(
    session: AgentSession, buffer: list[dict]
) -> metrics.UsageCollector:
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics_collected(ev: MetricsCollectedEvent) -> None:
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)
        try:
            buffer.append(
                {
                    "type": type(ev.metrics).__name__,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "data": _serialize_metric(ev.metrics),
                }
            )
        except Exception as e:
            logger.warning(f"Failed to capture metric: {e}")

    return usage_collector


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
) -> None:
    @ctx.room.local_participant.register_rpc_method("start_turn")
    async def start_turn(data: rtc.RpcInvocationData) -> str:
        logger.info(f"start_turn RPC called by {data.caller_identity}")
        session.interrupt()
        session.clear_user_turn()
        if getattr(session, "room_io", None) is not None:
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


async def _start_auto_session(
    ctx: agents.JobContext,
    session: AgentSession,
    config: RuntimeConfig,
    agent: VoiceAssistantAgent,
) -> None:
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=_build_room_options(),
    )
    logger.info("Interview coaching auto session started")
    await session.generate_reply(instructions=config.initial_reply)


async def _start_ptt_session(
    ctx: agents.JobContext,
    session: AgentSession,
    config: RuntimeConfig,
    agent: VoiceAssistantAgent,
) -> None:
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=_build_room_options(),
    )

    session.input.set_audio_enabled(False)
    _register_push_to_talk_rpcs(ctx, session)
    logger.info("Interview coaching PTT session started")
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
        return

    report_dict: dict = {}
    try:
        report = ctx.make_session_report()
        report_dict = report.to_dict()
        report_dict.setdefault("started_at", report.started_at)
        report_dict.setdefault("duration", report.duration)
    except Exception as e:
        logger.warning(f"Failed to create session report: {e}")

    usage_summary: dict | None = None
    if state.usage_collector is not None:
        try:
            summary = state.usage_collector.get_summary()
            usage_summary = (
                summary.model_dump() if hasattr(summary, "model_dump") else dict(summary)
            )
        except Exception as e:
            logger.warning(f"Failed to build usage summary: {e}")

    try:
        await finalize_recording(
            config=state.config,
            lk_api=ctx.api,
            egress_id=state.egress_id,
            agent_type="interview-agent",
            agent_name=REGISTERED_AGENT_NAME,
            room_name=state.room_name,
            audio_url=state.audio_url,
            audio_s3_key=state.audio_s3_key,
            report_dict=report_dict,
            resolved_user_id=state.resolved_user_id,
            participant_identity=state.participant_identity,
            phone_number=state.phone_number,
            metrics_events=state.metrics_events,
            usage_summary=usage_summary,
        )
    except Exception as e:
        logger.error(f"Recording finalization failed: {e}")

    flush_langfuse()


@server.rtc_session(agent_name=REGISTERED_AGENT_NAME, on_session_end=on_session_end)
async def entrypoint(ctx: agents.JobContext) -> None:
    config = build_runtime_config()
    room_metadata = ctx.job.room.metadata or ctx.room.metadata
    metadata = parse_room_metadata(room_metadata)
    mode = resolve_interaction_mode(metadata)
    session_config = extract_session_config(metadata)
    recording_metadata = build_recording_metadata(metadata, mode)
    await ctx.connect()
    register_idle_room_watchdog(ctx)

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        logger.info(f"User disconnected: {participant.identity}")

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

    agent = VoiceAssistantAgent(
        instructions=agent_instructions,
        language_style=language_info["language_style"],
    )

    session = build_agent_session(config, mode, session_config, language_info)
    metrics_buffer: list[dict] = []
    usage_collector = _attach_metrics_logging(session, metrics_buffer)

    rec_cfg = build_recording_config()
    if rec_cfg.enabled:
        try:
            audio_url, audio_s3_key, egress_id = await start_recording(
                config=rec_cfg,
                lk_api=ctx.api,
                agent_type="interview-agent",
                agent_name=config.agent_name,
                room_name=ctx.room.name,
                resolved_user_id=resolved_user_id,
                participant_identity=participant_identity,
                phone_number=phone_number,
                metadata=recording_metadata,
            )
            _recording_sessions[ctx.room.name] = RecordingSessionState(
                config=rec_cfg,
                egress_id=egress_id,
                room_name=ctx.room.name,
                audio_url=audio_url,
                audio_s3_key=audio_s3_key,
                resolved_user_id=resolved_user_id,
                participant_identity=participant_identity,
                phone_number=phone_number,
                metrics_events=metrics_buffer,
                usage_collector=usage_collector,
            )
        except Exception as e:
            logger.error(f"Failed to initialize recording: {e}")

    if mode is InteractionMode.PTT:
        await _start_ptt_session(ctx, session, config, agent)
    else:
        await _start_auto_session(ctx, session, config, agent)


if __name__ == "__main__":
    agents.cli.run_app(server)
