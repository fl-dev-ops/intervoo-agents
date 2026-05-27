from __future__ import annotations

import logging
from typing import Any

from livekit.agents import function_tool

from memory import VALID_CATEGORIES, ensure_user_entity, search_memories

logger = logging.getLogger(__name__)


def build_memory_tools(memory_client: Any, user_id: str | None):
    @function_tool(
        name="recall_memory",
        description=(
            "Search past conversations for relevant facts about this user. Use this "
            "only when remembered context would materially improve the conversation."
        ),
    )
    async def recall_memory(query: str) -> str:
        if user_id is None:
            return "Memory system not available for this user."
        try:
            results = await search_memories(memory_client, query, user_id)
            memories = [
                item.get("memory", "")
                for item in results.get("results", [])
                if isinstance(item, dict) and item.get("memory")
            ]
            if not memories:
                return "No relevant memories found."
            return "Relevant memories:\n" + "\n\n".join(memories)
        except Exception as e:
            logger.warning("Failed to recall memory: %s", e)
            return "I couldn't retrieve memories at the moment."

    @function_tool(
        name="save_memory",
        description=(
            "Save a durable, concise fact about the user for future conversations. "
            "Use only for stable preferences, background, goals, skills, or outcomes."
        ),
    )
    async def save_memory(content: str, category: str = "preference") -> str:
        if user_id is None:
            return "Memory system not available for this user."

        normalized_content = content.strip() if isinstance(content, str) else ""
        normalized_category = category.strip() if isinstance(category, str) else ""
        if not normalized_content:
            return "No memory saved because content was empty."
        if normalized_category not in VALID_CATEGORIES:
            return (
                "No memory saved because category must be one of: "
                + ", ".join(sorted(VALID_CATEGORIES))
            )

        try:
            await ensure_user_entity(memory_client, user_id)
            await memory_client.add(
                [{"role": "user", "content": normalized_content}],
                user_id=user_id,
                metadata={"category": normalized_category},
                infer=False,
            )
            return "Memory saved."
        except Exception as e:
            logger.warning("Failed to save memory: %s", e)
            return "I couldn't save that memory at the moment."

    return [recall_memory, save_memory]
