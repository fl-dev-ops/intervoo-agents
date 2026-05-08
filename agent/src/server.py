from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from profile import (
    AgentProfile,
    ProfileError,
    load_profile_catalog,
    pick_profile,
)
from typing import Any

from dotenv import load_dotenv
from livekit import agents, rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    MetricsCollectedEvent,
    metrics,
    room_io,
)
from livekit.agents.beta.tools import EndCallTool
from livekit.plugins import noise_cancellation

from identity import (
    resolve_phone_number_from_call_context,
    resolve_user_id_from_call_context,
    resolve_user_id_from_room_metadata,
)
from kb_tools import build_kb_tool
from knowledge_base import (
    ChromaKnowledgeBase,
    build_knowledge_base_config,
    with_collection,
)
from language import resolve_language_config
from prompt import build_prompt_context, load_prompt, render_prompt
from recording_config import RecordingConfig, build_recording_config
from recording_db import init_pool
from recording_runtime import finalize_recording, start_recording
from session import InteractionMode, SessionConfig, build_agent_session
from tracing import flush_langfuse, setup_langfuse
from unified_agent import UnifiedAgent
from watchdog import cancel_idle_room_watchdog, register_idle_room_watchdog

logger = logging.getLogger("intervoo_agent")

CALLER_LOOKUP_TIMEOUT_SECONDS = 5
DEFAULT_AGENT_NAME = "intervoo-agent"
MAX_CONCURRENT_SESSIONS = 10

END_CALL_EXTRA_DESCRIPTION = (
    "Only end the call when the user clearly indicates the conversation is complete."
)
END_CALL_INSTRUCTIONS = "Thanks for practicing with me today. Goodbye."

APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_CONFIG_PATH = APP_DIR / "config" / "agents.json"

load_dotenv(str(APP_DIR / ".env.local"))
load_dotenv(str(APP_DIR / ".env"))


def _resolve_profile_config_path() -> Path:
    override = os.getenv("AGENT_PROFILE_CONFIG")
    if override:
        return Path(override)
    return DEFAULT_PROFILE_CONFIG_PATH


_profile_catalog = load_profile_catalog(_resolve_profile_config_path())
_kb_base_config = build_knowledge_base_config()
REGISTERED_AGENT_NAME = os.getenv("AGENT_NAME", DEFAULT_AGENT_NAME)


@dataclass(frozen=True)
class SessionState:
    profile: AgentProfile
    room_name: str
    resolved_user_id: str | None
    participant_identity: str | None
    phone_number: str | None
    webhook_url: str | None
    recording_config: RecordingConfig | None = None
    recording_session_id: str | None = None
    egress_id: str | None = None
    audio_url: str | None = None
    audio_s3_key: str | None = None


_sessions: dict[str, SessionState] = {}


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

    dict_id = raw_config.get("dict_id") or raw_config.get("dictId")
    normalized_dict_id = (
        dict_id.strip() if isinstance(dict_id, str) and dict_id.strip() else None
    )

    speaking_speed = raw_config.get("speakingSpeed")
    normalized_speaking_speed: float | None = None
    if isinstance(speaking_speed, (int, float)) and math.isfinite(speaking_speed):
        normalized_speaking_speed = float(speaking_speed)
    elif isinstance(speaking_speed, str):
        try:
            parsed = float(speaking_speed)
        except ValueError:
            parsed = None
        if parsed is not None and math.isfinite(parsed):
            normalized_speaking_speed = parsed

    return SessionConfig(
        voice=normalized_voice,
        speaking_speed=normalized_speaking_speed,
        dict_id=normalized_dict_id,
    )


def resolve_interaction_mode(metadata: Mapping[str, object] | None) -> InteractionMode:
    if not metadata:
        return InteractionMode.AUTO
    interaction_mode = metadata.get("interaction_mode") or metadata.get(
        "interactionMode"
    )
    if isinstance(interaction_mode, str):
        normalized = interaction_mode.strip().lower()
        if normalized == "ptt":
            return InteractionMode.PTT
        if normalized == "auto":
            return InteractionMode.AUTO
    return InteractionMode.AUTO


def build_recording_metadata(
    room_metadata: Mapping[str, object] | None,
    mode: InteractionMode,
    profile: AgentProfile,
) -> dict[str, object]:
    metadata = dict(room_metadata) if room_metadata else {}
    metadata["interaction_mode"] = mode.value
    metadata["agent_id"] = profile.id
    return metadata


def _build_end_call_tool() -> EndCallTool:
    return EndCallTool(
        extra_description=END_CALL_EXTRA_DESCRIPTION,
        delete_room=True,
        end_instructions=END_CALL_INSTRUCTIONS,
    )


def _build_kb(profile: AgentProfile) -> ChromaKnowledgeBase | None:
    if not profile.kb_collection or not _kb_base_config.enabled:
        return None
    config = with_collection(_kb_base_config, profile.kb_collection)
    return ChromaKnowledgeBase(config)


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
    agent: UnifiedAgent,
) -> None:
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=_build_room_options(),
    )
    logger.info("Unified agent auto session started")


async def _start_ptt_session(
    ctx: agents.JobContext,
    session: AgentSession,
    agent: UnifiedAgent,
) -> None:
    await session.start(
        room=ctx.room,
        agent=agent,
        room_options=_build_room_options(),
    )
    session.input.set_audio_enabled(False)
    _register_push_to_talk_rpcs(ctx, session)
    logger.info("Unified agent PTT session started")


def _pick_call_participant(ctx: agents.JobContext) -> rtc.RemoteParticipant | None:
    participants = list(ctx.room.remote_participants.values())
    for participant in participants:
        if participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return participant
    return participants[0] if participants else None


async def _resolve_call_state(
    ctx: agents.JobContext,
    initial_user_id: str,
) -> tuple[str, str | None, str | None, dict[str, str] | None]:
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
    return resolved_user_id, participant_identity, phone_number, participant_attributes


def _compute_worker_load(current_server: AgentServer) -> float:
    return 1.0 if len(current_server.active_jobs) >= MAX_CONCURRENT_SESSIONS else 0.0


server = AgentServer(
    shutdown_process_timeout=60,
    load_fnc=_compute_worker_load,
    load_threshold=0.5,
)


async def _post_webhook(
    webhook_url: str,
    payload: dict[str, Any],
) -> None:
    from urllib import error, request

    def _send() -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        logger.info("Posting webhook to %s", webhook_url)
        req = request.Request(
            webhook_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(req, timeout=30) as response:
            status = getattr(response, "status", response.getcode())
            if status >= 400:
                raise RuntimeError(f"Webhook returned status {status}")

    try:
        await asyncio.to_thread(_send)
        logger.info("Webhook delivered to %s", webhook_url)
    except error.HTTPError as e:
        response_body = ""
        try:
            response_body = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        logger.error(
            "Webhook failed for %s with HTTP %s: %s%s",
            webhook_url,
            e.code,
            e.reason,
            f" body={response_body}" if response_body else "",
        )
    except Exception as e:
        logger.error("Webhook delivery failed for %s: %s", webhook_url, e)


async def on_session_end(ctx: agents.JobContext) -> None:
    cancel_idle_room_watchdog(ctx.room.name)

    state = _sessions.pop(ctx.room.name, None)
    if state is None:
        logger.info("No session state found for room %s", ctx.room.name)
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

    recording_result: dict[str, object] | None = None

    if state.recording_config is not None:
        try:
            recording_result = await finalize_recording(
                config=state.recording_config,
                lk_api=ctx.api,
                egress_id=state.egress_id,
                session_id=state.recording_session_id,
                agent_type=state.profile.agent_type,
                agent_name=state.profile.agent_type,
                room_name=state.room_name,
                audio_url=state.audio_url or "",
                audio_s3_key=state.audio_s3_key or "",
                report_dict=report_dict,
                resolved_user_id=state.resolved_user_id,
                participant_identity=state.participant_identity,
                phone_number=state.phone_number,
                webhook_url=state.webhook_url,
                send_webhook=False,
            )
        except Exception as e:
            logger.error(f"Recording finalization failed: {e}")

    if state.webhook_url:
        try:
            transcript_data = (
                recording_result.get("transcript") if recording_result else None
            )
            if transcript_data is None:
                try:
                    from recording_transcript import normalize_session_report

                    transcript_data = normalize_session_report(
                        report_dict,
                        agent_type=state.profile.agent_type,
                        agent_name=state.profile.agent_type,
                        resolved_user_id=state.resolved_user_id,
                        participant_identity=state.participant_identity,
                        phone_number=state.phone_number,
                    )
                except Exception:
                    pass

            payload = {
                "agent_id": state.profile.id,
                "agent_type": state.profile.agent_type,
                "room_name": state.room_name,
                "audio_url": recording_result.get("audio_url")
                if recording_result
                else state.audio_url,
                "transcript_url": recording_result.get("transcript_url")
                if recording_result
                else None,
                "video_url": None,
                "transcript": transcript_data,
                "duration_ms": recording_result.get("duration_ms")
                if recording_result
                else None,
                "status": recording_result.get("status")
                if recording_result
                else "COMPLETED",
            }
            await _post_webhook(state.webhook_url, payload)
        except Exception as e:
            logger.error(f"Failed to post completion webhook: {e}")

    flush_langfuse()


@server.rtc_session(agent_name=REGISTERED_AGENT_NAME, on_session_end=on_session_end)
async def entrypoint(ctx: agents.JobContext) -> None:
    room_metadata = ctx.job.room.metadata or ctx.room.metadata
    metadata = parse_room_metadata(room_metadata)

    try:
        profile = pick_profile(_profile_catalog, metadata)
    except ProfileError as e:
        logger.error(f"Cannot resolve agent profile: {e}")
        return

    mode = resolve_interaction_mode(metadata)
    session_config = extract_session_config(metadata)
    recording_metadata = build_recording_metadata(metadata, mode, profile)
    await ctx.connect()
    register_idle_room_watchdog(ctx)

    @ctx.room.on("participant_disconnected")
    def on_participant_disconnected(participant: rtc.RemoteParticipant) -> None:
        logger.info(f"User disconnected: {participant.identity}")

    initial_user_id = resolve_user_id_from_room_metadata(room_metadata)
    (
        resolved_user_id,
        participant_identity,
        phone_number,
        participant_attributes,
    ) = await _resolve_call_state(ctx, initial_user_id)

    try:
        setup_langfuse(
            metadata={
                "langfuse.session.id": ctx.room.name,
                "langfuse.user.id": resolved_user_id or "anonymous",
                "agent_id": profile.id,
                "agent_name": profile.agent_type,
                "job_id": ctx.job.id,
                "mode": mode.value,
            }
        )
    except Exception as e:
        logger.warning(f"Langfuse setup failed: {e}")

    try:
        prompt_template = load_prompt(profile.prompt_url)
    except Exception as e:
        logger.error(f"Failed to load prompt for agent_id={profile.id}: {e}")
        return

    prompt_context = build_prompt_context(metadata)
    agent_instructions = render_prompt(prompt_template, context=prompt_context)

    comfortable_language: str | None = None
    raw_prompt_context = metadata.get("prompt_context") or metadata.get("promptContext")
    if isinstance(raw_prompt_context, Mapping):
        candidate = raw_prompt_context.get("comfortableLanguage")
        if isinstance(candidate, str) and candidate.strip():
            comfortable_language = candidate.strip()
    language_info = resolve_language_config(comfortable_language)

    tools: list[Any] = []
    if profile.end_call_enabled:
        tools.append(_build_end_call_tool())

    kb = _build_kb(profile)
    if kb is not None:
        tools.append(build_kb_tool(profile.kb_shape, kb))

    memory_client = None
    if profile.memory_enabled:
        try:
            from memory import AsyncMemoryClient

            memory_client = AsyncMemoryClient()
        except Exception as e:
            logger.warning(f"Failed to initialize mem0 client: {e}")

    agent = UnifiedAgent(
        instructions=agent_instructions,
        tools=tools,
        initial_reply=profile.initial_reply,
        memory_client=memory_client,
        user_id=resolved_user_id,
        participant_identity=participant_identity,
        participant_attributes=participant_attributes,
        room_name=ctx.room.name,
    )

    session = build_agent_session(
        tts_speaker=profile.voice_speaker,
        tts_dict_id=profile.voice_dict_id,
        mode=mode,
        session_config=session_config,
        language_info=language_info,
    )
    _attach_metrics_logging(session)

    webhook_url_raw = metadata.get("webhook_url") or metadata.get("webhookUrl")
    webhook_url = (
        webhook_url_raw.strip()
        if isinstance(webhook_url_raw, str) and webhook_url_raw.strip()
        else None
    )

    rec_cfg = build_recording_config()
    recording_session_id: str | None = None
    audio_url: str | None = None
    audio_s3_key: str | None = None
    egress_id: str | None = None

    if rec_cfg.enabled:
        try:
            if rec_cfg.database_url:
                try:
                    await init_pool(rec_cfg.database_url)
                except Exception as e:
                    logger.error(f"Failed to initialize recording DB: {e}")
            (
                recording_session_id,
                audio_url,
                audio_s3_key,
                egress_id,
            ) = await start_recording(
                config=rec_cfg,
                lk_api=ctx.api,
                agent_type=profile.agent_type,
                agent_name=profile.agent_type,
                room_name=ctx.room.name,
                resolved_user_id=resolved_user_id,
                participant_identity=participant_identity,
                phone_number=phone_number,
                metadata=recording_metadata,
            )
        except Exception as e:
            logger.error(f"Failed to initialize recording: {e}")

    _sessions[ctx.room.name] = SessionState(
        profile=profile,
        room_name=ctx.room.name,
        resolved_user_id=resolved_user_id,
        participant_identity=participant_identity,
        phone_number=phone_number,
        webhook_url=webhook_url,
        recording_config=rec_cfg if rec_cfg.enabled else None,
        recording_session_id=recording_session_id,
        egress_id=egress_id,
        audio_url=audio_url,
        audio_s3_key=audio_s3_key,
    )

    if mode is InteractionMode.PTT:
        await _start_ptt_session(ctx, session, agent)
    else:
        await _start_auto_session(ctx, session, agent)


def main() -> None:
    agents.cli.run_app(server)


if __name__ == "__main__":
    main()
