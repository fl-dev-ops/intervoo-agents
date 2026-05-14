from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from livekit import agents, rtc
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    MetricsCollectedEvent,
    function_tool,
    metrics,
    room_io,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import noise_cancellation, openai, sarvam, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

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
    PROMPT_VERSION,
    REGISTERED_AGENT_NAME,
)
from identity import (
    resolve_phone_number_from_call_context,
    resolve_user_id_from_call_context,
    resolve_user_id_from_room_metadata,
)
from knowledge_base import (
    ChromaKnowledgeBase,
    build_knowledge_base_config,
    retrieve_knowledge_from_base,
)
from language import resolve_language_config, resolve_stt_mode
from prompt import build_prompt_context, render_prompt
from recording_config import RecordingConfig, build_recording_config
from recording_db import init_pool
from recording_runtime import finalize_recording, start_recording
from tracing import flush_langfuse, setup_langfuse
from watchdog import cancel_idle_room_watchdog, register_idle_room_watchdog

logger = logging.getLogger("interview_coaching_agent")
MAX_CONCURRENT_SESSIONS = 10
_knowledge_base_config = build_knowledge_base_config()
_knowledge_base = ChromaKnowledgeBase(_knowledge_base_config)
QUESTION_FILTER_KEYS = ("content_type", "domain", "band")


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
    sarvam_tts_dict_id: str | None = DEFAULT_SARVAM_TTS_DICT_ID
    initial_reply: str = INITIAL_REPLY


@dataclass(frozen=True)
class RecordingSessionState:
    config: RecordingConfig
    session_id: str | None
    egress_id: str | None
    room_name: str
    audio_url: str
    audio_s3_key: str
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
        deepgram_stt_language=values.get(
            "DEEPGRAM_STT_LANGUAGE", DEFAULT_DEEPGRAM_STT_LANGUAGE
        ),
        deepgram_stt_model=values.get("DEEPGRAM_STT_MODEL", DEFAULT_DEEPGRAM_STT_MODEL),
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


def extract_question_filter_defaults(
    metadata: Mapping[str, object] | None,
) -> dict[str, object]:
    if not metadata:
        return {}

    defaults: dict[str, object] = {}
    raw_filters = metadata.get("questionFilters") or metadata.get("question_filters")
    if isinstance(raw_filters, Mapping):
        for key in QUESTION_FILTER_KEYS:
            value = raw_filters.get(key)
            if value is not None:
                defaults[key] = value

    if "band" not in defaults:
        band = metadata.get("selectedBand")
        if band is not None:
            defaults["band"] = band

    return defaults


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


def _normalize_question_type(value: object) -> list[str]:
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    if isinstance(value, str) and value.strip():
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _normalize_question_record(record: dict[str, object]) -> dict[str, object] | None:
    metadata = (
        record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    )
    question_type = _normalize_question_type(metadata.get("question_type"))
    if not question_type:
        question_type_json = metadata.get("question_type_json")
        if isinstance(question_type_json, str):
            try:
                question_type = _normalize_question_type(json.loads(question_type_json))
            except json.JSONDecodeError:
                question_type = []

    question_id = record.get("id")
    question_text = record.get("text")
    if not isinstance(question_id, str) or not isinstance(question_text, str):
        return None
    if not question_id.strip() or not question_text.strip() or not question_type:
        return None

    band = metadata.get("band")
    if isinstance(band, str):
        try:
            band = int(band)
        except ValueError:
            band = None

    return {
        "id": question_id.strip(),
        "text": question_text.strip(),
        "question_type": question_type,
        "category": metadata.get("category"),
        "difficulty_level": metadata.get("difficulty_level"),
        "band": band if isinstance(band, int) else None,
    }


async def _publish_question_list_event(
    room: rtc.Room,
    *,
    batch_number: int,
    records: list[dict[str, object]],
    query: str,
    filters: dict[str, object],
) -> None:
    questions: list[dict[str, object]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        question = _normalize_question_record(record)
        if question is not None:
            questions.append(question)

    payload = {
        "type": f"question_list_{batch_number}",
        "status": "retrieved",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "batch_number": batch_number,
            "query": query,
            "filters": filters,
            "questions": questions,
        },
    }

    try:
        await room.local_participant.publish_data(
            json.dumps(payload).encode("utf-8"),
            reliable=True,
        )
        logger.info(
            "Published question_list_%d (%d questions)",
            batch_number,
            len(questions),
        )
    except Exception as e:
        logger.error(f"Failed to publish question_list event: {e}")


async def _publish_question_started_event(
    room: rtc.Room,
    *,
    question: dict[str, object],
) -> None:
    payload = {
        "type": "diagnostic_question_started",
        "status": "started",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": {
            "question": question,
        },
    }

    await room.local_participant.publish_data(
        json.dumps(payload).encode("utf-8"),
        reliable=True,
    )
    logger.info("Published diagnostic_question_started for %s", question["id"])


def build_diagnostic_question_tools(
    room: rtc.Room | None,
    default_filters: Mapping[str, object] | None = None,
):
    batch_counter = {"value": 0}
    questions_by_id: dict[str, dict[str, object]] = {}
    session_filters = {
        key: value
        for key, value in (default_filters or {}).items()
        if key in QUESTION_FILTER_KEYS and value is not None
    }

    @function_tool(
        name="retrieve_knowledge",
        description=(
            "Retrieve relevant records from the configured knowledge base. For this "
            "diagnostic agent, records are assessment questions. Use filters for "
            "stage, domain, difficulty, band, and content_type when known. Only ask "
            "questions returned by this tool."
        ),
    )
    async def retrieve_knowledge(
        query: str,
        content_type: str | None = None,
        domain: str | None = None,
        category: str | None = None,
        difficulty_level: str | list[str] | None = None,
        band: int | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        call_filters = {
            key: value
            for key, value in {
                "content_type": content_type,
                "domain": domain,
                "category": category,
                "difficulty_level": difficulty_level,
                "band": band,
            }.items()
            if value is not None
        }
        logger.info(f"call_filters {call_filters}, session_filters {session_filters}")
        filters = {**call_filters, **session_filters}
        logger.info(f"filters {filters}")
        result = await retrieve_knowledge_from_base(
            _knowledge_base,
            query=query,
            filters=filters or None,
            exclude_ids=exclude_ids,
            limit=limit,
        )

        records = result.get("records") if isinstance(result, dict) else None
        if (
            isinstance(result, dict)
            and result.get("status") == "ok"
            and isinstance(records, list)
        ):
            for record in records:
                if not isinstance(record, dict):
                    continue
                question = _normalize_question_record(record)
                if question is not None:
                    questions_by_id[str(question["id"])] = question

        if (
            room is not None
            and isinstance(result, dict)
            and result.get("status") == "ok"
            and isinstance(records, list)
            and records
        ):
            batch_counter["value"] += 1
            await _publish_question_list_event(
                room,
                batch_number=batch_counter["value"],
                records=records,
                query=query,
                filters=filters,
            )

        return result

    @function_tool(
        name="mark_question_started",
        description=(
            "Call this immediately before asking any diagnostic question returned "
            "by retrieve_knowledge. It publishes the full question metadata to the "
            "frontend and returns the exact question text to ask."
        ),
    )
    async def mark_question_started(question_id: str) -> dict[str, object]:
        normalized_id = question_id.strip() if isinstance(question_id, str) else ""
        question = questions_by_id.get(normalized_id)
        if question is None:
            return {
                "status": "not_found",
                "message": (
                    "Question id was not found in retrieved records. Call "
                    "retrieve_knowledge first and use one of its returned ids."
                ),
            }

        if room is not None:
            try:
                await _publish_question_started_event(room, question=question)
            except Exception as e:
                logger.error(f"Failed to publish question started event: {e}")
                return {
                    "status": "publish_failed",
                    "question_id": normalized_id,
                    "message": "Question could not be published to the frontend.",
                }

        return {
            "status": "ok",
            "question_id": normalized_id,
            "question_text": question["text"],
            "question": question,
        }

    return retrieve_knowledge, mark_question_started


class VoiceAssistantAgent(Agent):
    def __init__(
        self,
        instructions: str | None = None,
        *,
        retrieve_tool=None,
        mark_question_started_tool=None,
        knowledge_base_enabled: bool | None = None,
    ) -> None:
        if retrieve_tool is None and mark_question_started_tool is None:
            if knowledge_base_enabled is None:
                knowledge_base_enabled = _knowledge_base_config.enabled
            if knowledge_base_enabled:
                retrieve_tool, mark_question_started_tool = build_diagnostic_question_tools(
                    None
                )

        tools = [build_end_call_tool()]
        if mark_question_started_tool is not None:
            tools.insert(0, mark_question_started_tool)
        if retrieve_tool is not None:
            tools.insert(0, retrieve_tool)

        super().__init__(
            instructions=instructions or render_prompt(),
            tools=tools,
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
        dict_id=config.sarvam_tts_dict_id,
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

    @ctx.room.local_participant.register_rpc_method("resume_session")
    async def resume_session(data: rtc.RpcInvocationData) -> str:
        logger.info(f"resume_session RPC called by {data.caller_identity}")
        session.input.set_audio_enabled(True)
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


def _prewarm(proc: agents.JobProcess) -> None:
    started_at = time.monotonic()
    logger.info("Starting knowledge base prewarm")
    _knowledge_base.prewarm()
    logger.info(
        "Knowledge base prewarm finished in %.2fs",
        time.monotonic() - started_at,
    )


server = AgentServer(
    initialize_process_timeout=60,
    shutdown_process_timeout=60,
    setup_fnc=_prewarm,
    load_fnc=_compute_worker_load,
    num_idle_processes=1,
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
            egress_id=state.egress_id,
            session_id=state.session_id,
            agent_type=REGISTERED_AGENT_NAME,
            agent_name=REGISTERED_AGENT_NAME,
            room_name=state.room_name,
            audio_url=state.audio_url,
            audio_s3_key=state.audio_s3_key,
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
    mode = resolve_interaction_mode(metadata)
    session_config = extract_session_config(metadata)
    question_filter_defaults = extract_question_filter_defaults(metadata)
    recording_metadata = build_recording_metadata(metadata, mode)
    recording_metadata["prompt_version"] = PROMPT_VERSION
    await ctx.connect()
    register_idle_room_watchdog(ctx)

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

    kb_cfg = _knowledge_base_config
    prompt_context = build_prompt_context(metadata)
    agent_instructions = render_prompt(context=prompt_context)
    retrieve_tool = None
    mark_question_started_tool = None
    if kb_cfg.enabled:
        retrieve_tool, mark_question_started_tool = build_diagnostic_question_tools(
            ctx.room,
            default_filters=question_filter_defaults,
        )
    agent = VoiceAssistantAgent(
        instructions=agent_instructions,
        retrieve_tool=retrieve_tool,
        mark_question_started_tool=mark_question_started_tool,
    )

    comfortable_language: str | None = None
    raw_prompt_context = metadata.get("prompt_context") or metadata.get("promptContext")
    if isinstance(raw_prompt_context, Mapping):
        candidate = raw_prompt_context.get("comfortableLanguage")
        if isinstance(candidate, str) and candidate.strip():
            comfortable_language = candidate.strip()
    language_info = resolve_language_config(comfortable_language)

    session = build_agent_session(config, mode, session_config, language_info)
    _attach_metrics_logging(session)

    rec_cfg = build_recording_config()
    if rec_cfg.enabled:
        try:
            if rec_cfg.database_url:
                try:
                    await init_pool(rec_cfg.database_url)
                except Exception as e:
                    logger.error(f"Failed to initialize recording DB: {e}")
            session_id, audio_url, audio_s3_key, egress_id = await start_recording(
                config=rec_cfg,
                lk_api=ctx.api,
                agent_type=REGISTERED_AGENT_NAME,
                agent_name=config.agent_name,
                room_name=ctx.room.name,
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
                audio_url=audio_url,
                audio_s3_key=audio_s3_key,
                resolved_user_id=resolved_user_id,
                participant_identity=participant_identity,
                phone_number=phone_number,
            )
        except Exception as e:
            logger.error(f"Failed to initialize recording: {e}")

    if mode is InteractionMode.PTT:
        await _start_ptt_session(ctx, session, config, agent)
    else:
        await _start_auto_session(ctx, session, config, agent)


if __name__ == "__main__":
    agents.cli.run_app(server)
