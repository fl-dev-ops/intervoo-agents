from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, replace
from typing import Any

import chromadb

logger = logging.getLogger(__name__)

MetadataValue = str | int | float | bool

_DIFFICULTY_LEVELS = ("easy", "medium", "hard")

_BACKUP_QUESTION_TYPES = {
    "opening": "Thinking, Language",
    "behavioral": "Thinking, Confidence",
    "closing": "Language, Confidence",
    "domain": "Thinking, Language",
}

_BACKUP_QUESTIONS = {
    "opening": {
        "easy": [
            "Tell me about a computer science concept you recently learned and explain it in simple terms.",
            "What is the difference between data and information?",
            "Describe one tool or technology you have used and what you used it for.",
        ],
        "medium": [
            "How would you explain the difference between an application and a system?",
            "Describe a time you debugged a technical issue. What steps did you follow?",
            "How do you decide whether to solve a problem manually or automate it with code?",
        ],
        "hard": [
            "Pick a technical topic you know well and explain the trade-offs involved in using it.",
            "Describe how you would break down an unfamiliar technical problem before writing code.",
            "How would you compare two different solutions when both appear to work?",
        ],
    },
    "behavioral": {
        "easy": [
            "Tell me about a time you had to learn something quickly for a task.",
            "Describe a situation where you worked with someone who had a different opinion.",
            "Tell me about a time you received feedback and how you responded.",
        ],
        "medium": [
            "Describe a time you had to make progress despite unclear requirements.",
            "Tell me about a situation where you had to balance quality with a deadline.",
            "Describe a mistake you made in a project and what you changed afterward.",
        ],
        "hard": [
            "Tell me about a time you challenged a technical decision. How did you handle the discussion?",
            "Describe a situation where your first approach failed and you had to change direction.",
            "Tell me about a time you had to communicate a complex problem to a non-technical person.",
        ],
    },
    "closing": {
        "easy": [
            "What kind of technical work are you most interested in doing next?",
            "Which skill would you most like to improve over the next few months?",
            "What type of project environment helps you do your best work?",
        ],
        "medium": [
            "Looking back at your recent work, what is one area where you have improved?",
            "What would you want a team lead to know about how you approach learning?",
            "How do you decide what to focus on when there are many things to improve?",
        ],
        "hard": [
            "What technical weakness are you actively working on, and how are you measuring progress?",
            "If you joined a team tomorrow, what would you do in your first week to become productive?",
            "What trade-off are you willing to make in your next role, and what trade-off would you avoid?",
        ],
    },
    "domain": {
        "easy": [
            "What is the difference between a process and a thread?",
            "Explain what a database index is used for.",
            "What happens when a user enters a URL in a browser and presses Enter?",
        ],
        "medium": [
            "How would you design a REST API endpoint for creating and updating a resource?",
            "Explain how you would find the cause of a slow database query.",
            "What are the trade-offs between using an array and a linked list?",
        ],
        "hard": [
            "How would you design a system that handles sudden spikes in traffic?",
            "Explain how transactions help maintain consistency in a database system.",
            "How would you reason about concurrency bugs in a multi-threaded program?",
        ],
    },
}


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
        collection="",
        default_limit=max(default_limit, 1),
    )


def with_collection(config: KnowledgeBaseConfig, collection: str) -> KnowledgeBaseConfig:
    return replace(config, collection=collection)


def _normalize_limit(limit: int | None, default_limit: int) -> int:
    if limit is None:
        return default_limit
    return max(int(limit), 1)


def _normalize_filter_value(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        scalar_values = [
            item
            for item in value
            if isinstance(item, (str, int, float, bool)) and item is not None
        ]
        if key == "band":
            coerced = []
            for item in scalar_values:
                if isinstance(item, str):
                    try:
                        coerced.append(int(item))
                    except ValueError:
                        pass
                elif isinstance(item, int):
                    coerced.append(item)
            scalar_values = coerced
        return {"$in": scalar_values} if scalar_values else None
    if isinstance(value, str):
        if key == "band":
            try:
                return int(value)
            except ValueError:
                return value
        return value
    if isinstance(value, (int, float, bool)):
        return value
    return None


def build_where_filter(filters: dict[str, Any] | None) -> dict[str, Any] | None:
    if not filters:
        return None

    clauses: list[dict[str, Any]] = []
    for key, value in filters.items():
        if not isinstance(key, str) or not key:
            continue
        normalized = _normalize_filter_value(value, key=key)
        if normalized is not None:
            clauses.append({key: normalized})

    if not clauses:
        return None
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def _filter_list(filters: dict[str, Any] | None, key: str) -> list[Any]:
    if not filters:
        return []
    value = filters.get(key)
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _backup_categories(filters: dict[str, Any] | None) -> list[tuple[str, str]]:
    values = _filter_list(filters, "category")
    if not values:
        return [(category, category) for category in _BACKUP_QUESTIONS]

    categories: list[tuple[str, str]] = []
    for value in values:
        if isinstance(value, str) and value in _BACKUP_QUESTIONS:
            categories.append((value, value))
    return categories


def _backup_difficulties(filters: dict[str, Any] | None) -> list[str]:
    values = _filter_list(filters, "difficulty_level")
    if not values:
        return list(_DIFFICULTY_LEVELS)

    difficulties: list[str] = []
    for value in values:
        if isinstance(value, str):
            difficulty = value.strip().lower()
            if difficulty in _DIFFICULTY_LEVELS:
                difficulties.append(difficulty)
    return difficulties


def _static_backup_records(
    *,
    filters: dict[str, Any] | None,
    exclude_ids: list[str] | None,
    limit: int,
) -> list[KnowledgeRecord]:
    excluded = {item for item in (exclude_ids or []) if isinstance(item, str)}
    content_type = (filters or {}).get("content_type") or "diagnostic_question"
    domain = (filters or {}).get("domain")
    band = (filters or {}).get("band")
    records: list[KnowledgeRecord] = []

    for category, metadata_category in _backup_categories(filters):
        category_questions = _BACKUP_QUESTIONS[category]
        for difficulty in _backup_difficulties(filters):
            for index, question in enumerate(category_questions[difficulty], start=1):
                record_id = f"backup:{category}:{difficulty}:{index}"
                if record_id in excluded:
                    continue

                metadata: dict[str, Any] = {
                    "content_type": content_type,
                    "category": metadata_category,
                    "difficulty_level": difficulty,
                    "question_type": _BACKUP_QUESTION_TYPES[category],
                    "source": "static_backup",
                }
                if isinstance(domain, str) and domain:
                    metadata["domain"] = domain
                if isinstance(band, int):
                    metadata["band"] = band

                records.append(
                    KnowledgeRecord(
                        id=record_id,
                        text=question,
                        metadata=metadata,
                    )
                )
                if len(records) >= limit:
                    return records

    return records


def _build_static_backup_response(
    knowledge_base: Any,
    *,
    filters: dict[str, Any] | None,
    exclude_ids: list[str] | None,
    limit: int | None,
) -> dict[str, Any]:
    config = getattr(knowledge_base, "_config", None)
    default_limit = getattr(config, "default_limit", 10)
    records = _static_backup_records(
        filters=filters,
        exclude_ids=exclude_ids,
        limit=_normalize_limit(limit, default_limit),
    )
    return build_knowledge_response(
        status="ok",
        records=records,
        message="Retrieved static backup knowledge base records.",
    )


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
            logger.error(
                f"[KB] Knowledge base not available: enabled={self._config.enabled}, "
                f"configured={self._config.configured}"
            )
            raise ValueError(
                "Knowledge base disabled or not configured: CHROMA_API_KEY, "
                "CHROMA_TENANT, CHROMA_DATABASE, and a per-persona collection are required"
            )

        if self._client is None:
            logger.info(
                f"[KB] Connecting to ChromaDB: tenant={self._config.tenant!r}, "
                f"database={self._config.database!r}"
            )
            self._client = chromadb.CloudClient(
                api_key=self._config.api_key,
                tenant=self._config.tenant,
                database=self._config.database,
            )

        logger.info(f"[KB] Getting collection: {self._config.collection!r}")
        self._collection = self._client.get_collection(self._config.collection)
        return self._collection

    def prewarm(self) -> None:
        if not self._config.available:
            return
        try:
            collection = self._get_collection()
            collection.query(query_texts=["prewarm"], n_results=1)
            logger.info("Knowledge base prewarmed successfully")
        except Exception as e:
            logger.warning(f"Knowledge base prewarm failed (non-fatal): {e}")

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
            logger.info("[KB] Empty query, returning no records")
            return []

        effective_limit = _normalize_limit(limit, self._config.default_limit)
        excluded = {item for item in (exclude_ids or []) if isinstance(item, str)}
        requested_limit = effective_limit + len(excluded)
        where_filter = build_where_filter(filters)
        collection = self._get_collection()

        logger.info(
            f"[KB] Querying ChromaDB: query={normalized_query!r}, "
            f"where={where_filter}, n_results={requested_limit}, "
            f"collection={self._config.collection}"
        )

        result = collection.query(
            query_texts=[normalized_query],
            n_results=requested_limit,
            where=where_filter,
            include=["documents", "metadatas", "distances"],
        )

        raw_ids = result.get("ids") or [[]]
        raw_count = len(raw_ids[0]) if raw_ids and raw_ids[0] else 0
        logger.info(f"[KB] ChromaDB returned {raw_count} raw results")

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
        logger.warning(f"[KB] Knowledge base retrieval failed: {e}", exc_info=True)
        return _build_static_backup_response(
            knowledge_base,
            filters=filters,
            exclude_ids=exclude_ids,
            limit=limit,
        )
    
    if not records:
        logger.info(f"[KB] No records returned for query={query!r}, filters={filters}")
        return build_knowledge_response(
            status="empty",
            message="No matching knowledge base records were found.",
        )

    logger.info(f"[KB] Returning {len(records)} records")
    return build_knowledge_response(
        status="ok",
        records=records,
        message="Retrieved knowledge base records.",
    )