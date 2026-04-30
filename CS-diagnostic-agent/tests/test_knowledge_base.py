from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from knowledge_base import (
    ChromaKnowledgeBase,
    KnowledgeBaseConfig,
    KnowledgeRecord,
    build_knowledge_base_config,
    build_knowledge_response,
    build_where_filter,
    retrieve_knowledge_from_base,
)


def test_build_knowledge_base_config_defaults() -> None:
    config = build_knowledge_base_config({})

    assert config.enabled is True
    assert config.collection == "diagnostic_questions"
    assert config.default_limit == 5
    assert config.configured is False
    assert config.available is False


def test_build_knowledge_base_config_honors_env() -> None:
    config = build_knowledge_base_config(
        {
            "ENABLE_KNOWLEDGE_BASE": "true",
            "CHROMA_API_KEY": "key",
            "CHROMA_TENANT": "tenant",
            "CHROMA_DATABASE": "database",
            "CHROMA_COLLECTION": "kb",
            "KNOWLEDGE_BASE_DEFAULT_LIMIT": "7",
        }
    )

    assert config.available is True
    assert config.collection == "kb"
    assert config.default_limit == 7


def test_build_knowledge_base_config_can_disable_retrieval() -> None:
    config = build_knowledge_base_config(
        {
            "ENABLE_KNOWLEDGE_BASE": "false",
            "CHROMA_API_KEY": "key",
            "CHROMA_TENANT": "tenant",
            "CHROMA_DATABASE": "database",
        }
    )

    assert config.enabled is False
    assert config.available is False


def test_build_where_filter_normalizes_exact_and_list_values() -> None:
    assert build_where_filter(
        {
            "content_type": "diagnostic_question",
            "difficulty_level": ["easy", "medium"],
            "band": 1,
        }
    ) == {
        "$and": [
            {"content_type": "diagnostic_question"},
            {"difficulty_level": {"$in": ["easy", "medium"]}},
            {"band": 1},
        ]
    }


def test_retrieve_queries_chroma_and_excludes_ids() -> None:
    collection = MagicMock()
    collection.query.return_value = {
        "ids": [["q001", "q002"]],
        "documents": [["Question one?", "Question two?"]],
        "metadatas": [[{"category": "domain"}, {"category": "domain"}]],
        "distances": [[0.12, 0.34]],
    }
    kb = ChromaKnowledgeBase(
        KnowledgeBaseConfig(
            api_key="key",
            tenant="tenant",
            database="database",
            collection="diagnostic_questions",
            default_limit=1,
        ),
        collection=collection,
    )

    records = kb.retrieve(
        query="React Node project",
        filters={"category": "domain"},
        exclude_ids=["q001"],
    )

    collection.query.assert_called_once_with(
        query_texts=["React Node project"],
        n_results=2,
        where={"category": "domain"},
        include=["documents", "metadatas", "distances"],
    )
    assert records == [
        KnowledgeRecord(
            id="q002",
            text="Question two?",
            metadata={"category": "domain"},
            distance=0.34,
        )
    ]


@pytest.mark.asyncio
async def test_retrieve_knowledge_from_base_returns_empty_status() -> None:
    kb = MagicMock()
    kb.retrieve_async = AsyncMock(return_value=[])

    response = await retrieve_knowledge_from_base(kb, query="missing")

    assert response == {
        "status": "empty",
        "records": [],
        "count": 0,
        "message": "No matching knowledge base records were found.",
    }


@pytest.mark.asyncio
async def test_retrieve_knowledge_from_base_returns_unavailable_status() -> None:
    kb = MagicMock()
    kb.retrieve_async = AsyncMock(side_effect=RuntimeError("network failed"))

    response = await retrieve_knowledge_from_base(kb, query="anything")

    assert response == {
        "status": "unavailable",
        "records": [],
        "count": 0,
        "message": "Knowledge base retrieval is unavailable right now.",
    }


def test_build_knowledge_response_serializes_records() -> None:
    response = build_knowledge_response(
        status="ok",
        records=[
            KnowledgeRecord(
                id="q001",
                text="What is an API?",
                metadata={"category": "domain"},
                distance=0.2,
            )
        ],
        message="done",
    )

    assert response == {
        "status": "ok",
        "records": [
            {
                "id": "q001",
                "text": "What is an API?",
                "metadata": {"category": "domain"},
                "distance": 0.2,
            }
        ],
        "count": 1,
        "message": "done",
    }
