from __future__ import annotations

import logging

from livekit.agents import function_tool

from knowledge_base import ChromaKnowledgeBase, retrieve_knowledge_from_base

logger = logging.getLogger(__name__)

def merge_diagnostic_filters(
    default_filters: dict[str, object] | None,
    tool_filters: dict[str, object | None],
) -> dict[str, object] | None:
    filters: dict[str, object] = {}
    if default_filters:
        filters.update(default_filters)

    filters.update({key: value for key, value in tool_filters.items() if value is not None})
    return filters or None


def make_simple_retrieve_knowledge(kb: ChromaKnowledgeBase):
    @function_tool(
        name="retrieve_knowledge",
        description="Retrieve relevant records from the configured knowledge base.",
    )
    async def retrieve_knowledge(
        query: str,
        filters: dict[str, object] | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        return await retrieve_knowledge_from_base(
            kb,
            query=query,
            filters=filters,
            exclude_ids=exclude_ids,
            limit=limit,
        )

    return retrieve_knowledge


def make_diagnostic_retrieve_knowledge(
    kb: ChromaKnowledgeBase,
    default_filters: dict[str, object] | None = None,
):
    @function_tool(
        name="retrieve_knowledge",
        description=(
            "Retrieve relevant records from the configured knowledge base. For this "
            "diagnostic agent, records are assessment questions. Use filters for "
            "stage, domain, difficulty, band, and content_type when known. Only ask "
            "questions returned by this tool."
        ),
    )
    async def retrieve_knowledge(
        query: str,
        content_type: str | None = None,
        domain: str | None = None,
        category: str | None = None,
        difficulty_level: str | list[str] | None = None,
        band: int | None = None,
        exclude_ids: list[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, object]:
        # Default filters take preference over agent tool calls
        filters = merge_diagnostic_filters(
            {
                "content_type": content_type,
                "domain": domain,
                "category": category,
                "difficulty_level": difficulty_level,
                "band": band,
            },
            default_filters,
        )
        logger.info(f"[KB] Retrieving knowledge: {query=}, {filters=}")
        return await retrieve_knowledge_from_base(
            kb,
            query=query,
            filters=filters,
            exclude_ids=exclude_ids,
            limit=limit,
        )

    return retrieve_knowledge


def build_kb_tool(
    shape: str,
    kb: ChromaKnowledgeBase,
    default_filters: dict[str, object] | None = None,
):
    if shape == "simple":
        return make_simple_retrieve_knowledge(kb)
    if shape == "diagnostic":
        return make_diagnostic_retrieve_knowledge(kb, default_filters)
    raise ValueError(f"Unknown kb shape: {shape!r}")
