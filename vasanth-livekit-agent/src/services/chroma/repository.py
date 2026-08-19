"""Chroma repository service for vector database operations."""

from __future__ import annotations

import logging
import os
import time
from collections.abc import MutableMapping
from typing import Any

logger = logging.getLogger(__name__)

LOG_PREFIX = "[EXT-API:chroma]"

USERDATA_CHROMA_CLIENT = "chroma_client"
USERDATA_CHROMA_COLLECTION = "chroma_collection"


def _chroma_settings() -> dict[str, str]:
    """Get Chroma configuration from environment."""
    return {
        "api_key": os.getenv("CHROMA_API_KEY", ""),
        "tenant": os.getenv("CHROMA_TENANT", ""),
        "database": os.getenv("CHROMA_DATABASE", ""),
        "collection": os.getenv("CHROMA_COLLECTION", ""),
    }


def chroma_runtime_identity() -> str:
    """Return the non-secret Chroma location used in worker startup logs."""
    settings = _chroma_settings()
    return (
        f"tenant={settings['tenant']!r} database={settings['database']!r} "
        f"collection={settings['collection']!r}"
    )


def chroma_configured() -> bool:
    """Check if Chroma is configured."""
    return all(_chroma_settings().values())


def get_cached_collection(userdata: MutableMapping[str, Any]) -> Any:
    """Return a process-cached Chroma collection."""
    collection = userdata.get(USERDATA_CHROMA_COLLECTION)
    if collection is not None:
        return collection

    settings = _chroma_settings()
    if not all(settings.values()):
        raise ValueError(
            "Chroma not configured: set CHROMA_API_KEY, CHROMA_TENANT, "
            "CHROMA_DATABASE and CHROMA_COLLECTION"
        )

    import chromadb

    started = time.monotonic()
    client = userdata.get(USERDATA_CHROMA_CLIENT)
    if client is None:
        client = chromadb.CloudClient(
            api_key=settings["api_key"],
            tenant=settings["tenant"],
            database=settings["database"],
        )
        userdata[USERDATA_CHROMA_CLIENT] = client

    collection = client.get_collection(settings["collection"])
    userdata[USERDATA_CHROMA_COLLECTION] = collection
    logger.info(
        "%s connected %s count=%d elapsed_ms=%d",
        LOG_PREFIX,
        chroma_runtime_identity(),
        collection.count(),
        int((time.monotonic() - started) * 1000),
    )
    return collection


def prewarm_chroma(userdata: MutableMapping[str, Any]) -> None:
    """Connect and query once so the embedder is warm before a session."""
    if not chroma_configured():
        return
    started = time.monotonic()
    try:
        collection = get_cached_collection(userdata)
        collection.count()
        logger.info(
            "%s prewarmed %s elapsed_ms=%d",
            LOG_PREFIX,
            chroma_runtime_identity(),
            int((time.monotonic() - started) * 1000),
        )
    except Exception as e:
        logger.warning("%s prewarm failed: %s", LOG_PREFIX, e)
