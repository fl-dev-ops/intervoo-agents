from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from livekit import api
from livekit.protocol.egress import (
    EgressStatus,
    EncodedFileOutput,
    EncodedFileType,
    ListEgressRequest,
    RoomCompositeEgressRequest,
    S3Upload,
    StopEgressRequest,
)

from recording_config import RecordingConfig
from recording_db import (
    insert_session,
    update_session_completed,
    update_session_finalizing,
)
from recording_store import (
    build_audio_s3_key,
    build_metrics_s3_key,
    build_s3_url,
    build_transcript_s3_key,
    build_verbose_s3_key,
    build_video_s3_key,
    upload_metrics_json,
    upload_transcript_json,
    upload_verbose_json,
)
from recording_transcript import (
    normalize_metrics_payload,
    normalize_session_report,
    normalize_verbose_payload,
)

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    EgressStatus.EGRESS_COMPLETE,
    EgressStatus.EGRESS_FAILED,
    EgressStatus.EGRESS_ABORTED,
    EgressStatus.EGRESS_LIMIT_REACHED,
}


def _build_s3_upload(config: RecordingConfig) -> S3Upload:
    if not config.s3_access_key or not config.s3_secret_key:
        raise ValueError(
            "AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY are required for "
            "LiveKit Cloud egress uploads"
        )
    kwargs: dict[str, Any] = {
        "access_key": config.s3_access_key,
        "secret": config.s3_secret_key,
        "region": config.s3_region,
        "bucket": config.s3_bucket,
    }
    if config.s3_endpoint:
        kwargs["endpoint"] = config.s3_endpoint
    if config.s3_force_path_style:
        kwargs["force_path_style"] = True
    return S3Upload(**kwargs)


async def start_recording(
    *,
    config: RecordingConfig,
    lk_api: api.LiveKitAPI,
    agent_type: str,
    agent_name: str,
    room_name: str,
    room_sid: str | None = None,
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
    str | None,
]:
    now = datetime.now(timezone.utc)
    audio_s3_key = build_audio_s3_key(agent_type, room_name, config.s3_base_prefix, now)
    audio_url = build_s3_url(
        config.s3_bucket, audio_s3_key, config.s3_region, config.s3_endpoint
    )
    video_s3_key = build_video_s3_key(agent_type, room_name, config.s3_base_prefix, now)
    video_url = build_s3_url(
        config.s3_bucket, video_s3_key, config.s3_region, config.s3_endpoint
    )

    audio_egress_id: str | None = None
    video_egress_id: str | None = None

    s3_upload = _build_s3_upload(config)

    async def _start_audio_egress() -> None:
        nonlocal audio_egress_id
        try:
            file_output = EncodedFileOutput(
                file_type=EncodedFileType.MP3,
                filepath=audio_s3_key,
                s3=s3_upload,
            )
            egress_info = await asyncio.wait_for(
                lk_api.egress.start_room_composite_egress(
                    RoomCompositeEgressRequest(
                        room_name=room_name,
                        audio_only=True,
                        file_outputs=[file_output],
                    )
                ),
                timeout=config.egress_start_timeout_seconds,
            )
            audio_egress_id = egress_info.egress_id
            logger.info(f"Started audio egress {audio_egress_id} for room {room_name}")
        except Exception as e:
            logger.error(f"Failed to start audio egress for room {room_name}: {e}")

    async def _start_video_egress() -> None:
        nonlocal video_egress_id
        try:
            file_output = EncodedFileOutput(
                file_type=EncodedFileType.MP4,
                filepath=video_s3_key,
                s3=s3_upload,
            )
            egress_info = await asyncio.wait_for(
                lk_api.egress.start_room_composite_egress(
                    RoomCompositeEgressRequest(
                        room_name=room_name,
                        audio_only=False,
                        file_outputs=[file_output],
                    )
                ),
                timeout=config.egress_start_timeout_seconds,
            )
            video_egress_id = egress_info.egress_id
            logger.info(f"Started video egress {video_egress_id} for room {room_name}")
        except Exception as e:
            logger.error(f"Failed to start video egress for room {room_name}: {e}")

    await asyncio.gather(_start_audio_egress(), _start_video_egress())

    if audio_egress_id is None:
        audio_url = None
        audio_s3_key = None
    if video_egress_id is None:
        video_url = None
        video_s3_key = None

    session_id: str | None = None
    if config.database_url:
        try:
            session_id = await asyncio.wait_for(
                insert_session(
                    agent_type=agent_type,
                    agent_name=agent_name,
                    livekit_room_name=room_name,
                    livekit_room_sid=room_sid,
                    egress_id=audio_egress_id,
                    resolved_user_id=resolved_user_id,
                    participant_identity=participant_identity,
                    phone_number=phone_number,
                    started_at=now,
                    audio_url=audio_url,
                    audio_s3_key=audio_s3_key,
                    video_url=video_url,
                    video_s3_key=video_s3_key,
                    video_egress_id=video_egress_id,
                    metadata=metadata,
                ),
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Failed to insert session row: {e}")

    return (
        session_id,
        audio_url,
        audio_s3_key,
        audio_egress_id,
        video_url,
        video_s3_key,
        video_egress_id,
    )


def _build_webhook_payload(
    *,
    agent_type: str,
    agent_name: str,
    room_name: str,
    egress_id: str | None,
    egress_status: str | None,
    final_status: str,
    audio_url: str | None,
    audio_s3_key: str | None,
    video_url: str | None,
    video_s3_key: str | None,
    transcript_url: str | None,
    transcript_s3_key: str | None,
    metrics_url: str | None,
    metrics_s3_key: str | None,
    verbose_url: str | None,
    verbose_s3_key: str | None,
    duration_ms: int | None,
    report_dict: dict[str, Any],
    transcript_data: dict[str, Any] | None,
    resolved_user_id: str | None,
    participant_identity: str | None,
    phone_number: str | None,
) -> dict[str, Any]:
    return {
        "agent_type": agent_type,
        "agent_name": agent_name,
        "room_name": room_name,
        "room_id": report_dict.get("room_id"),
        "job_id": report_dict.get("job_id"),
        "status": final_status,
        "egress_id": egress_id,
        "egress_status": egress_status,
        "audio_url": audio_url,
        "audio_s3_key": audio_s3_key,
        "video_url": video_url,
        "video_s3_key": video_s3_key,
        "transcript_url": transcript_url,
        "transcript_s3_key": transcript_s3_key,
        "metrics_url": metrics_url,
        "metrics_s3_key": metrics_s3_key,
        "verbose_url": verbose_url,
        "verbose_s3_key": verbose_s3_key,
        "duration_ms": duration_ms,
        "started_at": transcript_data.get("session", {}).get("started_at")
        if transcript_data
        else None,
        "ended_at": transcript_data.get("session", {}).get("ended_at")
        if transcript_data
        else None,
        "resolved_user_id": resolved_user_id,
        "participant_identity": participant_identity,
        "phone_number": phone_number,
        "transcript": transcript_data,
    }


async def _post_completion_webhook(
    config: RecordingConfig,
    payload: dict[str, Any],
    webhook_url: str | None = None,
) -> None:
    target_url = webhook_url or config.webhook_url
    if not target_url:
        return

    def _send() -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        webhook_request = request.Request(
            target_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with request.urlopen(webhook_request, timeout=10) as response:
            status_code = getattr(response, "status", response.getcode())
            if status_code >= 400:
                raise RuntimeError(f"Webhook returned status {status_code}")

    try:
        await asyncio.to_thread(_send)
        logger.info("Recording webhook delivered to %s", target_url)
    except error.HTTPError as e:
        logger.error("Recording webhook failed with HTTP %s: %s", e.code, e.reason)
    except Exception as e:
        logger.error(f"Recording webhook delivery failed: {e}")


async def _stop_and_poll_egress(
    *,
    lk_api: api.LiveKitAPI,
    egress_id: str,
    timeout: int,
    label: str,
) -> tuple[str | None, bool, bool]:
    try:
        await lk_api.egress.stop_egress(StopEgressRequest(egress_id=egress_id))
        logger.info(f"Sent stop_egress for {label} {egress_id}")
    except Exception as e:
        logger.warning(f"Failed to stop {label} egress {egress_id}: {e}")

    egress_status_str: str | None = None
    failed = False
    timed_out = False
    poll_interval = 2
    elapsed = 0
    while elapsed < timeout:
        try:
            resp = await lk_api.egress.list_egress(
                ListEgressRequest(egress_id=egress_id)
            )
            if resp.items:
                info = resp.items[0]
                egress_status_str = EgressStatus.Name(info.status)
                if info.status in TERMINAL_STATUSES:
                    failed = info.status in (
                        EgressStatus.EGRESS_FAILED,
                        EgressStatus.EGRESS_ABORTED,
                    )
                    break
        except Exception as e:
            logger.warning(f"Error polling {label} egress {egress_id}: {e}")
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
    else:
        timed_out = True
        logger.warning(
            f"{label.title()} egress {egress_id} did not reach terminal state "
            f"within {timeout}s"
        )

    return egress_status_str, failed, timed_out


async def finalize_recording(
    *,
    config: RecordingConfig,
    lk_api: api.LiveKitAPI,
    session_id: str | None = None,
    egress_id: str | None,
    agent_type: str,
    agent_name: str,
    room_name: str,
    audio_url: str | None,
    audio_s3_key: str | None,
    report_dict: dict[str, Any],
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
    metrics_events: list[dict[str, Any]] | None = None,
    usage_summary: dict[str, Any] | None = None,
    webhook_url: str | None = None,
    send_webhook: bool = True,
    video_egress_id: str | None = None,
    video_url: str | None = None,
    video_s3_key: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)

    if session_id:
        try:
            await update_session_finalizing(session_id)
        except Exception as e:
            logger.warning(f"Failed to mark session finalizing: {e}")

    egress_status_str: str | None = None
    final_status = "COMPLETED" if egress_id else "EGRESS_START_FAILED"

    egress_tasks = []
    if egress_id:
        egress_tasks.append(
            _stop_and_poll_egress(
                lk_api=lk_api,
                egress_id=egress_id,
                timeout=config.egress_poll_timeout_seconds,
                label="audio",
            )
        )
    if video_egress_id:
        egress_tasks.append(
            _stop_and_poll_egress(
                lk_api=lk_api,
                egress_id=video_egress_id,
                timeout=config.egress_poll_timeout_seconds,
                label="video",
            )
        )

    if egress_tasks:
        egress_results = await asyncio.gather(*egress_tasks)
        audio_result = egress_results[0] if egress_id else None
        if audio_result is not None:
            egress_status_str, audio_failed, audio_timed_out = audio_result
            if audio_failed:
                final_status = "EGRESS_FAILED"
            elif audio_timed_out:
                final_status = "FINALIZE_TIMEOUT"

        if video_egress_id:
            video_result = egress_results[-1]
            _, video_failed, _ = video_result
            if video_failed:
                logger.warning(
                    f"Video egress {video_egress_id} failed/aborted, "
                    "continuing with audio-only recording"
                )

    duration_ms: int | None = None
    started_at = report_dict.get("started_at")
    if started_at:
        duration = report_dict.get("duration")
        duration_ms = (
            int(duration * 1000)
            if duration
            else int((now.timestamp() - started_at) * 1000)
        )

    transcript_data: dict[str, Any] | None = None
    transcript_url: str | None = None
    transcript_s3_key: str | None = None
    try:
        transcript_data = normalize_session_report(
            report_dict,
            agent_type=agent_type,
            agent_name=agent_name,
            egress_id=egress_id,
            egress_status=egress_status_str,
            resolved_user_id=resolved_user_id,
            participant_identity=participant_identity,
            phone_number=phone_number,
        )
        transcript_s3_key = build_transcript_s3_key(
            agent_type, room_name, config.s3_base_prefix, now
        )
        transcript_url = await asyncio.wait_for(
            asyncio.to_thread(
                upload_transcript_json,
                config,
                transcript_s3_key,
                transcript_data,
            ),
            timeout=config.s3_upload_timeout_seconds,
        )
    except Exception as e:
        logger.error(f"Failed to upload transcript: {e}")

    metrics_url: str | None = None
    metrics_s3_key: str | None = None
    try:
        metrics_payload = normalize_metrics_payload(
            report_dict,
            agent_type=agent_type,
            agent_name=agent_name,
            egress_id=egress_id,
            egress_status=egress_status_str,
            resolved_user_id=resolved_user_id,
            participant_identity=participant_identity,
            phone_number=phone_number,
            events=metrics_events,
            usage_summary=usage_summary,
        )
        metrics_s3_key = build_metrics_s3_key(
            agent_type, room_name, config.s3_base_prefix, now
        )
        metrics_url = await asyncio.wait_for(
            asyncio.to_thread(
                upload_metrics_json,
                config,
                metrics_s3_key,
                metrics_payload,
            ),
            timeout=config.s3_upload_timeout_seconds,
        )
    except Exception as e:
        logger.error(f"Failed to upload metrics: {e}")

    verbose_url: str | None = None
    verbose_s3_key: str | None = None
    try:
        verbose_payload = normalize_verbose_payload(
            report_dict,
            agent_type=agent_type,
            agent_name=agent_name,
            egress_id=egress_id,
            egress_status=egress_status_str,
            resolved_user_id=resolved_user_id,
            participant_identity=participant_identity,
            phone_number=phone_number,
        )
        verbose_s3_key = build_verbose_s3_key(
            agent_type, room_name, config.s3_base_prefix, now
        )
        verbose_url = await asyncio.wait_for(
            asyncio.to_thread(
                upload_verbose_json,
                config,
                verbose_s3_key,
                verbose_payload,
            ),
            timeout=config.s3_upload_timeout_seconds,
        )
    except Exception as e:
        logger.error(f"Failed to upload verbose report: {e}")

    if session_id:
        try:
            await update_session_completed(
                session_id,
                ended_at=now,
                duration_ms=duration_ms,
                transcript_url=transcript_url,
                transcript_s3_key=transcript_s3_key,
                metrics_url=metrics_url,
                metrics_s3_key=metrics_s3_key,
                verbose_url=verbose_url,
                verbose_s3_key=verbose_s3_key,
                video_url=video_url,
                video_s3_key=video_s3_key,
                egress_status=egress_status_str,
                egress_error=(
                    "Audio egress did not start"
                    if final_status == "EGRESS_START_FAILED"
                    else None
                ),
                status=final_status,
            )
        except Exception as e:
            logger.error(f"Failed to update session to {final_status}: {e}")

    if send_webhook and final_status == "COMPLETED" and transcript_url:
        await _post_completion_webhook(
            config,
            _build_webhook_payload(
                agent_type=agent_type,
                agent_name=agent_name,
                room_name=room_name,
                egress_id=egress_id,
                egress_status=egress_status_str,
                final_status=final_status,
                audio_url=audio_url,
                audio_s3_key=audio_s3_key,
                video_url=video_url,
                video_s3_key=video_s3_key,
                transcript_url=transcript_url,
                transcript_s3_key=transcript_s3_key,
                metrics_url=metrics_url,
                metrics_s3_key=metrics_s3_key,
                verbose_url=verbose_url,
                verbose_s3_key=verbose_s3_key,
                duration_ms=duration_ms,
                report_dict=report_dict,
                transcript_data=transcript_data,
                resolved_user_id=resolved_user_id,
                participant_identity=participant_identity,
                phone_number=phone_number,
            ),
            webhook_url=webhook_url,
        )

    logger.info(
        f"Recording finalized for room {room_name}: status={final_status}, "
        f"egress={egress_status_str}, transcript={'yes' if transcript_url else 'no'}, "
        f"metrics={'yes' if metrics_url else 'no'}, "
        f"verbose={'yes' if verbose_url else 'no'}"
    )

    return {
        "status": final_status,
        "egress_status": egress_status_str,
        "audio_url": audio_url,
        "audio_s3_key": audio_s3_key,
        "video_url": video_url,
        "video_s3_key": video_s3_key,
        "transcript_url": transcript_url,
        "transcript_s3_key": transcript_s3_key,
        "metrics_url": metrics_url,
        "metrics_s3_key": metrics_s3_key,
        "verbose_url": verbose_url,
        "verbose_s3_key": verbose_s3_key,
        "duration_ms": duration_ms,
        "transcript": transcript_data,
    }
