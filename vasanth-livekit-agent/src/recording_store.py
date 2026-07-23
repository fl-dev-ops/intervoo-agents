from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.config import Config

from recording_config import RecordingConfig

logger = logging.getLogger(__name__)

_s3_client = None
_s3_client_key: tuple[str, ...] | None = None


def _get_s3_client(config: RecordingConfig):
    global _s3_client, _s3_client_key
    client_key = (
        config.s3_access_key,
        config.s3_secret_key,
        config.s3_region,
        config.s3_endpoint,
        str(config.s3_force_path_style),
    )
    if _s3_client is not None and _s3_client_key == client_key:
        return _s3_client

    kwargs: dict = {
        "region_name": config.s3_region,
        "config": Config(
            connect_timeout=5,
            read_timeout=config.s3_upload_timeout_seconds,
            retries={"max_attempts": 2, "mode": "standard"},
            s3={"addressing_style": ("path" if config.s3_force_path_style else "auto")},
        ),
    }
    if config.s3_access_key and config.s3_secret_key:
        kwargs["aws_access_key_id"] = config.s3_access_key
        kwargs["aws_secret_access_key"] = config.s3_secret_key
    if config.s3_endpoint:
        kwargs["endpoint_url"] = config.s3_endpoint

    _s3_client = boto3.client("s3", **kwargs)
    _s3_client_key = client_key
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


def build_video_s3_key(
    agent_type: str,
    room_name: str,
    base_prefix: str = "agents",
    now: datetime | None = None,
) -> str:
    return build_s3_key(agent_type, room_name, "video.mp4", base_prefix, now)


def build_transcript_s3_key(
    agent_type: str,
    room_name: str,
    base_prefix: str = "agents",
    now: datetime | None = None,
) -> str:
    return build_s3_key(agent_type, room_name, "transcript.json", base_prefix, now)


def build_metrics_s3_key(
    agent_type: str,
    room_name: str,
    base_prefix: str = "agents",
    now: datetime | None = None,
) -> str:
    return build_s3_key(agent_type, room_name, "metrics.json", base_prefix, now)


def build_verbose_s3_key(
    agent_type: str,
    room_name: str,
    base_prefix: str = "agents",
    now: datetime | None = None,
) -> str:
    return build_s3_key(agent_type, room_name, "verbose.json", base_prefix, now)


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


def upload_metrics_json(
    config: RecordingConfig,
    s3_key: str,
    metrics_data: dict,
) -> str:
    client = _get_s3_client(config)
    body = json.dumps(metrics_data, indent=2, default=str)
    client.put_object(
        Bucket=config.s3_bucket,
        Key=s3_key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    url = build_s3_url(config.s3_bucket, s3_key, config.s3_region, config.s3_endpoint)
    logger.info(f"Uploaded metrics to s3://{config.s3_bucket}/{s3_key}")
    return url


def upload_verbose_json(
    config: RecordingConfig,
    s3_key: str,
    verbose_data: dict,
) -> str:
    client = _get_s3_client(config)
    body = json.dumps(verbose_data, indent=2, default=str)
    client.put_object(
        Bucket=config.s3_bucket,
        Key=s3_key,
        Body=body.encode("utf-8"),
        ContentType="application/json",
    )
    url = build_s3_url(config.s3_bucket, s3_key, config.s3_region, config.s3_endpoint)
    logger.info(f"Uploaded verbose report to s3://{config.s3_bucket}/{s3_key}")
    return url
