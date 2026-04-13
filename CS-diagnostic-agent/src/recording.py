from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

import boto3
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

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0"
TERMINAL_STATUSES = {
    EgressStatus.EGRESS_COMPLETE,
    EgressStatus.EGRESS_FAILED,
    EgressStatus.EGRESS_ABORTED,
    EgressStatus.EGRESS_LIMIT_REACHED,
}

_s3_client = None


@dataclass(frozen=True)
class RecordingConfig:
    s3_egress_enabled: bool = True
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint: str = ""
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_force_path_style: bool = False
    s3_base_prefix: str = "agents"
    webhook_url: str = ""
    egress_poll_timeout_seconds: int = 45

    @property
    def enabled(self) -> bool:
        return bool(self.s3_egress_enabled and self.s3_bucket)


def build_recording_config(env: dict[str, str] | None = None) -> RecordingConfig:
    values = os.environ if env is None else env
    return RecordingConfig(
        s3_egress_enabled=values.get("ENABLE_RECORDING", "true").lower()
        in ("1", "true", "yes"),
        s3_bucket=values.get("AWS_S3_BUCKET", ""),
        s3_region=values.get("AWS_DEFAULT_REGION", "us-east-1"),
        s3_endpoint=values.get("AWS_S3_ENDPOINT", ""),
        s3_access_key=values.get("AWS_ACCESS_KEY_ID", ""),
        s3_secret_key=values.get("AWS_SECRET_ACCESS_KEY", ""),
        s3_force_path_style=values.get("AWS_S3_FORCE_PATH_STYLE", "").lower()
        in ("1", "true", "yes"),
        s3_base_prefix=values.get("S3_BASE_PREFIX", "agents"),
        webhook_url=values.get("WEBHOOK_URL", ""),
        egress_poll_timeout_seconds=int(
            values.get("EGRESS_POLL_TIMEOUT_SECONDS", "45")
        ),
    )


def _get_s3_client(config: RecordingConfig):
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    kwargs: dict = {
        "aws_access_key_id": config.s3_access_key,
        "aws_secret_access_key": config.s3_secret_key,
        "region_name": config.s3_region,
    }
    if config.s3_endpoint:
        kwargs["endpoint_url"] = config.s3_endpoint
    if config.s3_force_path_style:
        kwargs["config"] = boto3.session.Config(s3={"addressing_style": "path"})

    _s3_client = boto3.client("s3", **kwargs)
    return _s3_client


def build_s3_key(
    agent_type: str,
    room_name: str,
    filename: str,
    base_prefix: str = "agents",
    now: datetime | None = None,
) -> str:
    ts = now or datetime.now(timezone.utc)
    date_path = ts.strftime("%Y/%m/%d")
    return f"{base_prefix}/{agent_type}/sessions/{date_path}/{room_name}/{filename}"


def build_s3_url(
    bucket: str,
    key: str,
    region: str = "us-east-1",
    endpoint: str = "",
) -> str:
    if endpoint:
        return f"{endpoint.rstrip('/')}/{bucket}/{key}"
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def build_audio_s3_key(
    agent_type: str,
    room_name: str,
    base_prefix: str = "agents",
    now: datetime | None = None,
) -> str:
    return build_s3_key(agent_type, room_name, "audio.mp3", base_prefix, now)


def build_transcript_s3_key(
    agent_type: str,
    room_name: str,
    base_prefix: str = "agents",
    now: datetime | None = None,
) -> str:
    return build_s3_key(agent_type, room_name, "transcript.json", base_prefix, now)


def upload_transcript_json(
    config: RecordingConfig,
    s3_key: str,
    transcript_data: dict,
) -> str:
    client = _get_s3_client(config)
    body = json.dumps(transcript_data, indent=2, default=str)
    client.put_object(
        Bucket=config.s3_bucket,
        Key=s3_key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    url = build_s3_url(config.s3_bucket, s3_key, config.s3_region, config.s3_endpoint)
    logger.info(f"Uploaded transcript to s3://{config.s3_bucket}/{s3_key}")
    return url


def _ts_to_iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif hasattr(item, "text"):
                parts.append(item.text)
        return " ".join(parts)
    return str(content) if content else ""


def _chat_history_items(report_dict: dict[str, Any]) -> list[dict[str, Any]]:
    chat_history = report_dict.get("chat_history", {})
    if not isinstance(chat_history, dict):
        return []

    raw_items = chat_history.get("items")
    if isinstance(raw_items, list):
        return [item for item in raw_items if isinstance(item, dict)]

    raw_messages = chat_history.get("messages")
    if isinstance(raw_messages, list):
        return [item for item in raw_messages if isinstance(item, dict)]

    return []


def normalize_session_report(
    report_dict: dict[str, Any],
    *,
    agent_type: str,
    agent_name: str,
    egress_id: str | None = None,
    egress_status: str | None = None,
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
) -> dict[str, Any]:
    session_started = report_dict.get("started_at")
    session_timestamp = report_dict.get("timestamp", time.time())
    duration = report_dict.get("duration")

    turns = []
    messages = _chat_history_items(report_dict)

    for idx, msg in enumerate(messages):
        if msg.get("type") not in (None, "message"):
            continue

        role = msg.get("role", "unknown")
        text = _extract_text(msg.get("content", ""))
        if not text:
            continue

        turn: dict[str, Any] = {
            "index": idx,
            "role": role,
            "text": text,
        }

        create_ts = msg.get("create_time") or msg.get("created_at")
        if create_ts:
            turn["timestamp"] = _ts_to_iso(create_ts)

        if msg.get("interrupted"):
            turn["interrupted"] = True

        tool_name = msg.get("tool_name")
        if tool_name:
            turn["tool_name"] = tool_name

        turns.append(turn)

    usage: dict[str, Any] = {}
    options = report_dict.get("options", {})
    if options:
        usage["model"] = options.get("llm", {}).get("model")

    events = report_dict.get("events", [])
    metadata_events = []
    for ev in events:
        if isinstance(ev, dict):
            metadata_events.append(
                {k: v for k, v in ev.items() if k in ("type", "timestamp")}
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "session": {
            "agent_type": agent_type,
            "agent_name": agent_name,
            "room": report_dict.get("room"),
            "room_id": report_dict.get("room_id"),
            "job_id": report_dict.get("job_id"),
            "egress_id": egress_id,
            "egress_status": egress_status,
            "started_at": _ts_to_iso(session_started),
            "ended_at": _ts_to_iso(session_timestamp),
            "duration_seconds": round(duration, 2) if duration else None,
        },
        "subject": {
            "resolved_user_id": resolved_user_id,
            "participant_identity": participant_identity,
            "phone_number": phone_number,
        },
        "turns": turns,
        "usage": usage,
        "metadata": {
            "event_count": len(events),
            "turn_count": len(turns),
            "events_summary": metadata_events[:20],
        },
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
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, str, str | None]:
    """Start egress recording and return audio destination details."""
    now = datetime.now(timezone.utc)
    audio_s3_key = build_audio_s3_key(agent_type, room_name, config.s3_base_prefix, now)
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
        egress_request = RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
            file=file_output,
        )
        egress_info = await lk_api.egress.start_room_composite_egress(egress_request)
        egress_id = egress_info.egress_id
        logger.info(f"Started egress {egress_id} for room {room_name}")
    except Exception as e:
        logger.error(f"Failed to start egress for room {room_name}: {e}")

    if metadata:
        logger.info(
            "Recording metadata captured for room %s: %s",
            room_name,
            json.dumps(metadata, default=str),
        )

    return audio_url, audio_s3_key, egress_id


def _build_webhook_payload(
    *,
    agent_type: str,
    agent_name: str,
    room_name: str,
    egress_id: str | None,
    egress_status: str | None,
    final_status: str,
    audio_url: str,
    audio_s3_key: str,
    transcript_url: str | None,
    transcript_s3_key: str | None,
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
        "transcript_url": transcript_url,
        "transcript_s3_key": transcript_s3_key,
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
    config: RecordingConfig, payload: dict[str, Any]
) -> None:
    if not config.webhook_url:
        return

    def _send() -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        webhook_request = request.Request(
            config.webhook_url,
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
        logger.info("Recording webhook delivered to %s", config.webhook_url)
    except error.HTTPError as e:
        logger.error("Recording webhook failed with HTTP %s: %s", e.code, e.reason)
    except Exception as e:
        logger.error(f"Recording webhook delivery failed: {e}")


async def finalize_recording(
    *,
    config: RecordingConfig,
    lk_api: api.LiveKitAPI,
    egress_id: str | None,
    agent_type: str,
    agent_name: str,
    room_name: str,
    audio_url: str,
    audio_s3_key: str,
    report_dict: dict[str, Any],
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
) -> None:
    """Finalize recording: stop egress, upload transcript, send webhook."""
    now = datetime.now(timezone.utc)

    egress_status_str: str | None = None
    final_status = "COMPLETED"

    if egress_id:
        try:
            await lk_api.egress.stop_egress(StopEgressRequest(egress_id=egress_id))
            logger.info(f"Sent stop_egress for {egress_id}")
        except Exception as e:
            logger.warning(f"Failed to stop egress {egress_id}: {e}")

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
                            final_status = "EGRESS_FAILED"
                        elif info.status == EgressStatus.EGRESS_ABORTED:
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

    duration_ms: int | None = None
    started_at = report_dict.get("started_at")
    if started_at:
        duration = report_dict.get("duration")
        if duration:
            duration_ms = int(duration * 1000)
        else:
            duration_ms = int((now.timestamp() - started_at) * 1000)

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
        transcript_url = upload_transcript_json(
            config, transcript_s3_key, transcript_data
        )
    except Exception as e:
        logger.error(f"Failed to upload transcript: {e}")

    if final_status == "COMPLETED" and transcript_url:
        try:
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
                    transcript_url=transcript_url,
                    transcript_s3_key=transcript_s3_key,
                    duration_ms=duration_ms,
                    report_dict=report_dict,
                    transcript_data=transcript_data,
                    resolved_user_id=resolved_user_id,
                    participant_identity=participant_identity,
                    phone_number=phone_number,
                ),
            )
        except Exception as e:
            logger.error(f"Failed to trigger recording webhook: {e}")

    logger.info(
        f"Recording finalized for room {room_name}: status={final_status}, "
        f"egress={egress_status_str}, transcript={'yes' if transcript_url else 'no'}, "
        f"webhook={'yes' if final_status == 'COMPLETED' and transcript_url and config.webhook_url else 'no'}"
    )
