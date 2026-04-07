from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import boto3

from recording_config import RecordingConfig

logger = logging.getLogger(__name__)

_s3_client = None


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
    url = build_s3_url(
        config.s3_bucket, s3_key, config.s3_region, config.s3_endpoint
    )
    logger.info(f"Uploaded transcript to s3://{config.s3_bucket}/{s3_key}")
    return url
