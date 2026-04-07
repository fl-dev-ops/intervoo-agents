from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from livekit import api
from livekit.protocol.egress import (
    EncodedFileOutput,
    EncodedFileType,
    EgressStatus,
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
    build_s3_url,
    build_transcript_s3_key,
    upload_transcript_json,
)
from recording_transcript import normalize_session_report

logger = logging.getLogger(__name__)

TERMINAL_STATUSES = {
    EgressStatus.EGRESS_COMPLETE,
    EgressStatus.EGRESS_FAILED,
    EgressStatus.EGRESS_ABORTED,
    EgressStatus.EGRESS_LIMIT_REACHED,
}


def _build_s3_upload(config: RecordingConfig) -> S3Upload:
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
) -> tuple[str | None, str | None]:
    """Start egress recording and insert a placeholder DB row.

    Returns (session_id, egress_id).
    """
    now = datetime.now(timezone.utc)
    audio_s3_key = build_audio_s3_key(
        agent_type, room_name, config.s3_base_prefix, now
    )
    audio_url = build_s3_url(
        config.s3_bucket, audio_s3_key, config.s3_region, config.s3_endpoint
    )

    egress_id: str | None = None
    try:
        s3_upload = _build_s3_upload(config)
        file_output = EncodedFileOutput(
            file_type=EncodedFileType.MP3,
            filepath=audio_s3_key,
            s3=s3_upload,
        )
        request = RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
            file=file_output,
        )
        egress_info = await lk_api.egress.start_room_composite_egress(request)
        egress_id = egress_info.egress_id
        logger.info(f"Started egress {egress_id} for room {room_name}")
    except Exception as e:
        logger.error(f"Failed to start egress for room {room_name}: {e}")

    session_id: str | None = None
    try:
        session_id = await insert_session(
            agent_type=agent_type,
            agent_name=agent_name,
            livekit_room_name=room_name,
            livekit_room_sid=room_sid,
            egress_id=egress_id,
            resolved_user_id=resolved_user_id,
            participant_identity=participant_identity,
            phone_number=phone_number,
            started_at=now,
            audio_url=audio_url,
            audio_s3_key=audio_s3_key,
            metadata=metadata,
        )
    except Exception as e:
        logger.error(f"Failed to insert session row: {e}")

    return session_id, egress_id


async def finalize_recording(
    *,
    config: RecordingConfig,
    lk_api: api.LiveKitAPI,
    session_id: str | None,
    egress_id: str | None,
    agent_type: str,
    agent_name: str,
    room_name: str,
    report_dict: dict[str, Any],
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
) -> None:
    """Finalize recording: upload transcript, stop egress, update DB."""
    now = datetime.now(timezone.utc)

    # 1. Mark finalizing
    if session_id:
        try:
            await update_session_finalizing(session_id)
        except Exception as e:
            logger.warning(f"Failed to mark session finalizing: {e}")

    # 2. Normalize and upload transcript
    transcript_url: str | None = None
    transcript_s3_key: str | None = None
    try:
        transcript_data = normalize_session_report(
            report_dict,
            agent_type=agent_type,
            agent_name=agent_name,
            egress_id=egress_id,
            resolved_user_id=resolved_user_id,
            participant_identity=participant_identity,
            phone_number=phone_number,
        )
        transcript_s3_key = build_transcript_s3_key(
            agent_type, room_name, config.s3_base_prefix, now
        )
        transcript_url = upload_transcript_json(
            config, transcript_s3_key, transcript_data
        )
    except Exception as e:
        logger.error(f"Failed to upload transcript: {e}")

    # 3. Stop egress and poll for completion
    egress_status_str: str | None = None
    egress_error: str | None = None
    final_status = "COMPLETED"

    if egress_id:
        try:
            await lk_api.egress.stop_egress(StopEgressRequest(egress_id=egress_id))
            logger.info(f"Sent stop_egress for {egress_id}")
        except Exception as e:
            logger.warning(f"Failed to stop egress {egress_id}: {e}")

        # Poll for terminal status
        timeout = config.egress_poll_timeout_seconds
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
                        if info.status == EgressStatus.EGRESS_FAILED:
                            egress_error = info.error or "Unknown egress failure"
                            final_status = "EGRESS_FAILED"
                        elif info.status == EgressStatus.EGRESS_ABORTED:
                            egress_error = "Egress aborted"
                            final_status = "EGRESS_FAILED"
                        break
            except Exception as e:
                logger.warning(f"Error polling egress {egress_id}: {e}")
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
        else:
            final_status = "FINALIZE_TIMEOUT"
            logger.warning(
                f"Egress {egress_id} did not reach terminal state within {timeout}s"
            )

    # 4. Compute duration
    duration_ms: int | None = None
    started_at = report_dict.get("started_at")
    if started_at:
        duration = report_dict.get("duration")
        if duration:
            duration_ms = int(duration * 1000)
        else:
            duration_ms = int((now.timestamp() - started_at) * 1000)

    # 5. Final DB update
    if session_id:
        try:
            await update_session_completed(
                session_id,
                ended_at=now,
                duration_ms=duration_ms,
                transcript_url=transcript_url,
                transcript_s3_key=transcript_s3_key,
                egress_status=egress_status_str,
                egress_error=egress_error,
                status=final_status,
            )
        except Exception as e:
            logger.error(f"Failed to update session to {final_status}: {e}")

    logger.info(
        f"Recording finalized for room {room_name}: status={final_status}, "
        f"egress={egress_status_str}, transcript={'yes' if transcript_url else 'no'}"
    )
