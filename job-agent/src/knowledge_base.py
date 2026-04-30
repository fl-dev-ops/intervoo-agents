from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

import chromadb

logger = logging.getLogger("job_finder_agent")

MetadataValue = str | int | float | bool


@dataclass(frozen=True)
class KnowledgeBaseConfig:
    enabled: bool = True
    api_key: str = ""
    tenant: str = ""
    database: str = ""
    collection: str = ""
    default_limit: int = 10

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.tenant and self.database and self.collection)

    @property
    def available(self) -> bool:
        return self.enabled and self.configured


@dataclass(frozen=True)
class KnowledgeRecord:
    id: str
    text: str
    metadata: dict[str, Any]
    distance: float | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "text": self.text,
            "metadata": self.metadata,
        }
        if self.distance is not None:
            payload["distance"] = self.distance
        return payload


def build_knowledge_base_config(
    env: dict[str, str] | None = None,
) -> KnowledgeBaseConfig:
    values = os.environ if env is None else env
    raw_limit = values.get("KNOWLEDGE_BASE_DEFAULT_LIMIT", "10")
    try:
        default_limit = int(raw_limit)
    except ValueError:
        default_limit = 10

    return KnowledgeBaseConfig(
        enabled=values.get("ENABLE_KNOWLEDGE_BASE", "true").lower()
        in ("1", "true", "yes"),
        api_key=values.get("CHROMA_API_KEY", ""),
        tenant=values.get("CHROMA_TENANT", ""),
        database=values.get("CHROMA_DATABASE", ""),
        collection=values.get("CHROMA_COLLECTION", ""),
        default_limit=max(default_limit, 1),
    )


def _normalize_limit(limit: int | None, default_limit: int) -> int:
    if limit is None:
        return default_limit
    return max(int(limit), 1)


def _normalize_filter_value(value: Any) -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        scalar_values = [
            item
            for item in value
            if isinstance(item, (str, int, float, bool)) and item is not None
        ]
        return {"$in": scalar_values} if scalar_values else None
    if isinstance(value, (str, int, float, bool)):
        return value
    return None


def build_where_filter(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    if not filters:
        return None

    clauses: list[dict[str, Any]] = []
    for key, value in filters.items():
        if not isinstance(key, str) or not key:
            continue
        normalized = _normalize_filter_value(value)
        if normalized is not None:
            clauses.append({key: normalized})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


class ChromaKnowledgeBase:
    def __init__(
        self,
        config: KnowledgeBaseConfig,
        *,
        client: Any | None = None,
        collection: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._collection = collection

    def _get_collection(self) -> Any:
        if self._collection is not None:
            return self._collection

        if not self._config.available:
            raise ValueError(
                "Knowledge base disabled or not configured: CHROMA_API_KEY, "
                "CHROMA_TENANT, CHROMA_DATABASE, and CHROMA_COLLECTION are required"
            )

        if self._client is None:
            self._client = chromadb.CloudClient(
                api_key=self._config.api_key,
                tenant=self._config.tenant,
                database=self._config.database,
            )

        self._collection = self._client.get_collection(self._config.collection)
        return self._collection

    def retrieve(
        self,
        *,
        query: str,
        filters: dict[str, Any] | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[KnowledgeRecord]:
        normalized_query = query.strip()
        if not normalized_query:
            return []

        effective_limit = _normalize_limit(limit, self._config.default_limit)
        excluded = {item for item in (exclude_ids or []) if isinstance(item, str)}
        requested_limit = effective_limit + len(excluded)
        collection = self._get_collection()
        result = collection.query(
            query_texts=[normalized_query],
            n_results=requested_limit,
            where=build_where_filter(filters),
            include=["documents", "metadatas", "distances"],
        )

        ids = (result.get("ids") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        records: list[KnowledgeRecord] = []

        for index, record_id in enumerate(ids):
            if record_id in excluded:
                continue
            document = documents[index] if index < len(documents) else ""
            metadata = metadatas[index] if index < len(metadatas) else {}
            distance = distances[index] if index < len(distances) else None
            records.append(
                KnowledgeRecord(
                    id=str(record_id),
                    text=str(document or ""),
                    metadata=dict(metadata or {}),
                    distance=float(distance) if distance is not None else None,
                )
            )
            if len(records) >= effective_limit:
                break

        return records

    async def retrieve_async(
        self,
        *,
        query: str,
        filters: dict[str, Any] | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> list[KnowledgeRecord]:
        return await asyncio.to_thread(
            self.retrieve,
            query=query,
            filters=filters,
            exclude_ids=exclude_ids,
            limit=limit,
        )


def build_knowledge_response(
    *,
    status: str,
    records: list[KnowledgeRecord] | None = None,
    message: str = "",
) -> dict[str, Any]:
    serialized_records = [record.to_dict() for record in (records or [])]
    return {
        "status": status,
        "records": serialized_records,
        "count": len(serialized_records),
        "message": message,
    }


async def retrieve_knowledge_from_base(
    knowledge_base: ChromaKnowledgeBase,
    *,
    query: str,
    filters: dict[str, Any] | None = None,
    exclude_ids: list[str] | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    try:
        records = await knowledge_base.retrieve_async(
            query=query,
            filters=filters,
            exclude_ids=exclude_ids,
            limit=limit,
        )
    except Exception as e:
        logger.warning(f"Knowledge base retrieval failed: {e}")
        return build_knowledge_response(
            status="unavailable",
            message="Knowledge base retrieval is unavailable right now.",
        )

    if not records:
        return build_knowledge_response(
            status="empty",
            message="No matching knowledge base records were found.",
        )

    return build_knowledge_response(
        status="ok",
        records=records,
        message="Retrieved knowledge base records.",
    )
