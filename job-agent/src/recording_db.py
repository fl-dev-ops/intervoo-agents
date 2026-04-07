from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import asyncpg

logger = logging.getLogger(__name__)

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS agent_sessions (
    id              TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    agent_type      TEXT NOT NULL,
    agent_name      TEXT NOT NULL,
    livekit_room_name TEXT NOT NULL,
    livekit_room_sid  TEXT,
    egress_id       TEXT,

    resolved_user_id    TEXT,
    participant_identity TEXT,
    phone_number        TEXT,

    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    duration_ms     INTEGER,
    status          TEXT NOT NULL DEFAULT 'RECORDING',
    egress_status   TEXT,
    egress_error    TEXT,

    audio_url       TEXT,
    audio_s3_key    TEXT,
    transcript_url  TEXT,
    transcript_s3_key TEXT,

    metadata        JSONB DEFAULT '{}'::jsonb,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_type_started
    ON agent_sessions (agent_type, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_agent_sessions_user_started
    ON agent_sessions (resolved_user_id, started_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_sessions_type_room
    ON agent_sessions (agent_type, livekit_room_name);
"""

_pool: asyncpg.Pool | None = None


async def init_pool(database_url: str) -> asyncpg.Pool:
    global _pool
    if _pool is not None:
        return _pool
    _pool = await asyncpg.create_pool(database_url, min_size=1, max_size=3)
    async with _pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)
    logger.info("Recording DB pool initialized and schema bootstrapped")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def insert_session(
    *,
    agent_type: str,
    agent_name: str,
    livekit_room_name: str,
    livekit_room_sid: str | None = None,
    egress_id: str | None = None,
    resolved_user_id: str | None = None,
    participant_identity: str | None = None,
    phone_number: str | None = None,
    started_at: datetime | None = None,
    audio_url: str | None = None,
    audio_s3_key: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> str:
    if _pool is None:
        raise RuntimeError("Recording DB pool not initialized")

    import json

    row = await _pool.fetchrow(
        """
        INSERT INTO agent_sessions (
            agent_type, agent_name, livekit_room_name, livekit_room_sid,
            egress_id, resolved_user_id, participant_identity, phone_number,
            started_at, status, audio_url, audio_s3_key, metadata
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,'RECORDING',$10,$11,$12::jsonb)
        ON CONFLICT (agent_type, livekit_room_name) DO UPDATE SET
            egress_id = EXCLUDED.egress_id,
            resolved_user_id = EXCLUDED.resolved_user_id,
            participant_identity = EXCLUDED.participant_identity,
            phone_number = EXCLUDED.phone_number,
            started_at = EXCLUDED.started_at,
            status = 'RECORDING',
            audio_url = EXCLUDED.audio_url,
            audio_s3_key = EXCLUDED.audio_s3_key,
            metadata = EXCLUDED.metadata,
            updated_at = now()
        RETURNING id
        """,
        agent_type,
        agent_name,
        livekit_room_name,
        livekit_room_sid,
        egress_id,
        resolved_user_id,
        participant_identity,
        phone_number,
        started_at or datetime.now(timezone.utc),
        audio_url,
        audio_s3_key,
        json.dumps(metadata or {}),
    )
    session_id = row["id"]
    logger.info(f"Inserted session {session_id} for room {livekit_room_name}")
    return session_id


async def update_session_finalizing(session_id: str) -> None:
    if _pool is None:
        return
    await _pool.execute(
        "UPDATE agent_sessions SET status='FINALIZING', updated_at=now() WHERE id=$1",
        session_id,
    )


async def update_session_completed(
    session_id: str,
    *,
    ended_at: datetime | None = None,
    duration_ms: int | None = None,
    transcript_url: str | None = None,
    transcript_s3_key: str | None = None,
    egress_status: str | None = None,
    egress_error: str | None = None,
    status: str = "COMPLETED",
    metadata: dict[str, Any] | None = None,
) -> None:
    if _pool is None:
        return

    import json

    await _pool.execute(
        """
        UPDATE agent_sessions SET
            ended_at = COALESCE($2, ended_at),
            duration_ms = COALESCE($3, duration_ms),
            transcript_url = COALESCE($4, transcript_url),
            transcript_s3_key = COALESCE($5, transcript_s3_key),
            egress_status = COALESCE($6, egress_status),
            egress_error = COALESCE($7, egress_error),
            status = $8,
            metadata = CASE WHEN $9::jsonb IS NOT NULL
                THEN metadata || $9::jsonb ELSE metadata END,
            updated_at = now()
        WHERE id = $1
        """,
        session_id,
        ended_at,
        duration_ms,
        transcript_url,
        transcript_s3_key,
        egress_status,
        egress_error,
        status,
        json.dumps(metadata) if metadata else None,
    )
    logger.info(f"Updated session {session_id} to {status}")
