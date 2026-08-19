"""Session lifecycle: on_session_end and entrypoint."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langfuse import get_client as get_langfuse_client
from livekit import agents, rtc
from livekit.agents import ConversationItemAddedEvent

from domains.screen import ScreenFeedbackRuntime
from domains.session import InteractionMode, build_agent_session
from infrastructure.config.profiles import ProfileError, pick_profile
from infrastructure.logging.langfuse import flush_langfuse, setup_langfuse
from infrastructure.monitoring.watchdog import (
    cancel_idle_room_watchdog,
    register_idle_room_watchdog,
)
from infrastructure.prompt import (
    build_prompt_context,
    extract_prompt_version,
    load_prompt,
    render_prompt,
)
from services.agent.unified import UnifiedAgent
from services.identity.resolver import resolve_user_id_from_room_metadata
from services.simulation import install_answer_submit_shim, merge_simulation_userdata
from domains.interview.evidence.tracker import InterviewEvidenceTracker
from domains.interview.questions.store import QuestionStore, normalize_supplied_questions
from infrastructure.config.resources import (
    get_or_create_turn_detector,
    get_prewarmed_turn_detector,
    get_profile_catalog,
    get_recording_config,
)

from .config import (
    CALLER_LOOKUP_TIMEOUT_SECONDS,
    EVIDENCE_TRACKER_CLOSE_TIMEOUT_SECONDS,
    MOCK_INTERVIEW_AGENT_TYPE,
    SCREEN_FEEDBACK_CLOSE_TIMEOUT_SECONDS,
    SessionState,
    StartupTimer,
    build_recording_metadata,
    extract_session_config,
    plan_line,
    parse_room_metadata,
    resolve_interaction_mode,
    resolve_profile_config_path,
    _sessions,
    _session_usage_loggers,
    _screen_feedback_runtimes,
)
from .metrics import attach_metrics_logging
from .recording import (
    finalize_recording_session,
    post_completion_webhook,
    start_recording_for_session,
    RecordingStartState,
)
from .session import resolve_call_state, start_auto_session, start_ptt_session
from .tools import build_end_call_tool, build_interview_tools, build_screen_tools

logger = logging.getLogger("intervoo_agent")


async def on_session_end(ctx: agents.JobContext) -> None:
    cancel_idle_room_watchdog(ctx.room.name)

    screen_feedback = _screen_feedback_runtimes.pop(ctx.room.name, None)
    if screen_feedback is not None:
        try:
            await asyncio.wait_for(screen_feedback.close(), timeout=SCREEN_FEEDBACK_CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("Timed out closing screen feedback runtime room=%s", ctx.room.name)
        except Exception:
            logger.exception("Failed to close screen feedback runtime room=%s", ctx.room.name)

    state = _sessions.pop(ctx.room.name, None)
    if state is None:
        logger.info("No session state found for room %s", ctx.room.name)
        flush_langfuse()
        return

    if state.evidence_tracker is not None:
        try:
            await asyncio.wait_for(state.evidence_tracker.close(), timeout=EVIDENCE_TRACKER_CLOSE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            logger.error("Timed out closing evidence tracker room=%s", ctx.room.name)
        except Exception:
            logger.exception("Failed to close evidence tracker room=%s", ctx.room.name)

    report_dict = _make_report(ctx)
    recording_result = await finalize_recording_session(state, ctx, report_dict)
    await post_completion_webhook(state, recording_result, report_dict)

    log_summary = _session_usage_loggers.pop(ctx.room.name, None)
    if log_summary is not None:
        log_summary()
    flush_langfuse()


def _make_report(ctx: agents.JobContext) -> dict:
    try:
        report = ctx.make_session_report()
        d = report.to_dict()
        d.setdefault("started_at", report.started_at)
        d.setdefault("duration", report.duration)
        return d
    except Exception as e:
        logger.warning(f"Failed to create session report: {e}")
        return {}


def _setup_prompt(metadata, profile, room_name: str, job_id: str):
    _prompt_version = extract_prompt_version(profile.prompt_url)
    try:
        setup_langfuse(metadata={
            "langfuse.session.id": room_name,
            "langfuse.user.id": "anonymous",
            "agent_id": profile.id,
            "agent_name": profile.agent_type,
            "job_id": job_id,
            "prompt_version": _prompt_version,
            "langfuse.prompt.name": "diagnostic-agent",
            "langfuse.prompt.label": _prompt_version,
        })
    except Exception as e:
        logger.warning(f"Langfuse setup failed: {e}")

    try:
        prompt_template = load_prompt(profile.prompt_url)
    except Exception as e:
        logger.error(f"Failed to load prompt for agent_id={profile.id}: {e}")
        return None, None

    prompt_context = build_prompt_context(metadata)
    question_store = QuestionStore()
    supplied = normalize_supplied_questions(metadata.get("questions"))
    if supplied:
        question_store.load(supplied)
        prompt_context["interview_plan"] = "\n".join(plan_line(q) for q in question_store.public_plan())
    elif isinstance(metadata.get("questions"), list):
        prompt_context["interview_plan"] = ""

    agent_instructions = render_prompt(prompt_template, context=prompt_context)
    try:
        lf = get_langfuse_client()
        lf.get_prompt("diagnostic-agent", label=_prompt_version, fallback=agent_instructions)
        lf.trace(id=room_name, metadata={"prompt_version": _prompt_version, "prompt_char_count": len(agent_instructions)})
    except Exception as e:
        logger.warning("Langfuse trace enrichment failed: %s", e)

    return agent_instructions, question_store


def _build_tools(ctx, question_store, participant_identity, evidence_tracker, evaluator_prompt, prompt_context, userdata, on_question_started, on_plan_loaded, profile, is_mock_interview, screen_feedback, timer_screen_feedback_enabled):
    tools = []
    if profile.end_call_enabled and not is_mock_interview:
        tools.append(build_end_call_tool())
    if profile.editor_events_enabled:
        tools.extend(build_interview_tools(
            ctx=ctx, question_store=question_store, participant_identity=participant_identity,
            evidence_tracker=evidence_tracker, evaluator_prompt=evaluator_prompt,
            prompt_context=prompt_context, userdata=userdata,
            on_question_started=on_question_started, on_plan_loaded=on_plan_loaded,
        ))
    if screen_feedback is not None and timer_screen_feedback_enabled:
        tools.extend(build_screen_tools(screen_feedback))
    return tools


async def entrypoint(ctx: agents.JobContext) -> None:
    timer = StartupTimer(ctx.room.name)
    userdata = ctx.proc.userdata
    room_metadata = ctx.job.room.metadata or ctx.room.metadata
    metadata = parse_room_metadata(room_metadata)
    simulation_ctx = ctx.simulation_context()
    metadata = merge_simulation_userdata(simulation_ctx, metadata)
    profile_catalog = get_profile_catalog(userdata, fallback_path=resolve_profile_config_path())

    try:
        profile = pick_profile(profile_catalog, metadata)
    except ProfileError as e:
        logger.error(f"Cannot resolve agent profile: {e}")
        return

    mode = resolve_interaction_mode(metadata)
    session_config = extract_session_config(metadata)
    recording_metadata = build_recording_metadata(metadata, mode, profile)
    timer.mark("metadata_profile")

    await ctx.connect()
    timer.mark("ctx_connect")
    register_idle_room_watchdog(ctx)

    @ctx.room.on("participant_disconnected")
    def on_disconnect(p: rtc.RemoteParticipant) -> None:
        logger.info(f"User disconnected: {p.identity}")

    initial_user_id = resolve_user_id_from_room_metadata(room_metadata)
    resolved_user_id, participant_identity, phone_number, _ = await resolve_call_state(ctx, initial_user_id)
    timer.mark("participant_lookup")

    if participant_identity is None:
        logger.warning("No participant joined within %ds room=%s", CALLER_LOOKUP_TIMEOUT_SECONDS, ctx.room.name)
        return

    agent_instructions, question_store = _setup_prompt(metadata, profile, ctx.room.name, ctx.job.id)
    if agent_instructions is None:
        return
    timer.mark("prompt_render")

    agent = None

    async def _inject_note(text, extra):
        if agent is not None:
            try:
                await agent.inject_internal_note(text, extra=extra)
            except Exception:
                logger.exception("Failed to inject note room=%s", ctx.room.name)

    async def _on_answer_submitted(qid):
        question_store.mark_answer_submitted(qid)
        await _inject_note(f"[Internal: candidate submitted answer for {qid}.]", {"internal_answer_submitted": qid})

    rec_cfg = get_recording_config(userdata)
    evidence_tracker = None
    evaluator_prompt = None
    is_mock = profile.agent_type == MOCK_INTERVIEW_AGENT_TYPE
    if is_mock:
        try:
            evaluator_prompt = load_prompt("prompts/interview/vasanth_evaluator.md")
        except Exception as e:
            logger.error("Failed to load evaluator prompt: %s", e)
            return
        evidence_tracker = InterviewEvidenceTracker(
            questions=question_store.internal_questions(),
            participant_identity=participant_identity,
            room_name=ctx.room.name,
            agent_type=profile.agent_type,
            recording_config=rec_cfg if rec_cfg.enabled else None,
            on_answer_submitted=_on_answer_submitted,
        )
        evidence_tracker.start(ctx.room)

    recording_task = None
    if rec_cfg.enabled:
        recording_task = asyncio.create_task(
            start_recording_for_session(config=rec_cfg, ctx=ctx, profile=profile, room_name=ctx.room.name,
                resolved_user_id=resolved_user_id, participant_identity=participant_identity,
                phone_number=phone_number, metadata=recording_metadata),
            name=f"recording-start:{ctx.room.name}",
        )

    screen_feedback_mode = metadata.get("screen_feedback_mode")
    timer_enabled = screen_feedback_mode == "timer"
    screen_feedback = None
    if profile.editor_events_enabled and timer_enabled:
        async def _on_screen_nudge(text):
            await _inject_note(text, {"internal_screen_nudge": True})
        screen_feedback = ScreenFeedbackRuntime(room=ctx.room, participant_identity=participant_identity,
            timer_enabled=timer_enabled, note_sink=_on_screen_nudge)

    session = build_agent_session(
        tts_speaker=profile.voice_speaker, tts_dict_id=profile.voice_dict_id,
        mode=mode, session_config=session_config,
        turn_detector=get_or_create_turn_detector(userdata) if mode is InteractionMode.AUTO else get_prewarmed_turn_detector(userdata),
        disable_preemptive_generation=profile.editor_events_enabled,
    )
    _session_usage_loggers[ctx.room.name] = attach_metrics_logging(session, ctx.room.name)

    if evidence_tracker is not None:
        @session.on("conversation_item_added")
        def on_item(event: ConversationItemAddedEvent):
            evidence_tracker.on_conversation_item(event.item)
        if simulation_ctx is not None:
            install_answer_submit_shim(session, evidence_tracker=evidence_tracker, metadata=metadata)

    async def _on_question_started(q):
        if evidence_tracker is not None:
            evidence_tracker.on_question_started(q)
        if screen_feedback is not None:
            await screen_feedback.on_question_started(q)

    tools = _build_tools(ctx, question_store, participant_identity, evidence_tracker,
        evaluator_prompt, dict(_prompt_context if '_prompt_context' in dir() else {}), userdata,
        _on_question_started, lambda n: evidence_tracker.load_plan(n) if evidence_tracker else None,
        profile, is_mock, screen_feedback, timer_enabled)

    if screen_feedback is not None and timer_enabled:
        agent_instructions += ("\n\nDuring an active coding question, use read_code_range followed by "
            "highlight_code, not inspect_shared_screen, before answering uncertainty or any request "
            "for a hint, doubt clarification, correctness check, or next step about editor code. "
            "Call inspect_shared_screen for an active whiteboard request or a coding request "
            "specifically about visual state outside the code editor. Never claim that you cannot "
            "see the candidate's screen. If the result includes candidate_message, say it and "
            "continue the interview. Treat screen_share_required, surface_unavailable, and loading "
            "as normal recoverable states; never call end_call because of them.")

    timer.mark("tool_build")

    agent = UnifiedAgent(instructions=agent_instructions, tools=tools, initial_reply=profile.initial_reply,
        participant_identity=participant_identity, room_name=ctx.room.name)
    timer.mark("session_build")

    webhook_url_raw = metadata.get("webhook_url")
    webhook_url = webhook_url_raw.strip() if isinstance(webhook_url_raw, str) and webhook_url_raw.strip() else None

    recording_start = await recording_task if recording_task is not None else RecordingStartState()
    timer.mark("recording_start")

    _sessions[ctx.room.name] = SessionState(
        profile=profile, room_name=ctx.room.name, resolved_user_id=resolved_user_id,
        participant_identity=participant_identity, phone_number=phone_number, webhook_url=webhook_url,
        recording_config=rec_cfg if rec_cfg.enabled else None,
        recording_session_id=recording_start.recording_session_id, egress_id=recording_start.egress_id,
        audio_url=recording_start.audio_url, audio_s3_key=recording_start.audio_s3_key,
        video_egress_id=recording_start.video_egress_id, video_url=recording_start.video_url,
        video_s3_key=recording_start.video_s3_key, evidence_tracker=evidence_tracker,
    )

    avatar_request = metadata.get("avatar")
    from services.agent.avatar import start_avatar
    await start_avatar(session, ctx.room, enabled=avatar_request is True or avatar_request == "liveavatar")
    timer.mark("avatar_start")

    if mode is InteractionMode.PTT:
        await start_ptt_session(ctx, session, agent)
    else:
        await start_auto_session(ctx, session, agent)

    if screen_feedback is not None:
        try:
            await screen_feedback.start(session)
            _screen_feedback_runtimes[ctx.room.name] = screen_feedback
        except Exception:
            logger.exception("Failed to start screen feedback room=%s", ctx.room.name)
            await screen_feedback.close()
    timer.mark("session_start")
