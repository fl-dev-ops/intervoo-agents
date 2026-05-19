from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from enum import Enum
from typing import Any, cast

from mem0 import AsyncMemoryClient
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

_openai_client: AsyncOpenAI | None = None
DEFAULT_MEMORY_MODEL = "openai/gpt-5.1"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class MemoryCategory(str, Enum):
    PERSONAL_INFO = "personal_info"
    LOCATION = "location"
    EDUCATION = "education"
    WORK_EXPERIENCE = "work_experience"
    JOB_INTEREST = "job_interest"
    SKILLS = "skills"
    SCREENING_RESULT = "screening_result"
    PREFERENCE = "preference"
    EVENT = "event"
    PERSONALITY = "personality"


VALID_CATEGORIES: set[str] = {c.value for c in MemoryCategory}

CATEGORY_DESCRIPTIONS: dict[str, str] = {
    MemoryCategory.PERSONAL_INFO: "Name, age, gender, basic identity details",
    MemoryCategory.LOCATION: "City, state, region, where the candidate is from or based",
    MemoryCategory.EDUCATION: "Degrees, institutions, field of study, year of completion",
    MemoryCategory.WORK_EXPERIENCE: "Past jobs, internships, work duration, responsibilities",
    MemoryCategory.JOB_INTEREST: "Target roles, career goals, industry or domain preference",
    MemoryCategory.SKILLS: "Technical skills, soft skills, certifications, languages spoken",
    MemoryCategory.SCREENING_RESULT: "CEFR level, screening outcome, assessment observations",
    MemoryCategory.PREFERENCE: "Work style preferences, likes, dislikes, habits",
    MemoryCategory.EVENT: "Upcoming plans, scheduled follow-ups, past session references",
    MemoryCategory.PERSONALITY: "Behavioral observations, confidence level, demeanor, attitude",
}

_CATEGORY_BLOCK = "\n".join(
    f"  - {name}: {desc}" for name, desc in CATEGORY_DESCRIPTIONS.items()
)

MEMORY_EXTRACTION_PROMPT = f"""You extract structured information from a candidate's messages during a job screening conversation.

Analyze the candidate's latest message in context and extract any noteworthy facts into one or more of the following FIXED categories:

{_CATEGORY_BLOCK}

Rules:
- Only use categories from the list above. Never invent new categories.
- Each extracted item must have a "category" (one of the exact category keys above) and "content" (a concise statement of the extracted fact).
- Content should be a short, self-contained statement. Write it as a third-person fact about the candidate.
- If the message contains nothing worth remembering (greetings, filler, yes/no with no new info), return an empty array.
- Do NOT extract information the assistant said — only extract from what the candidate/user said.
- A single message can produce multiple items across different categories.

Return a JSON object with a single key "items" containing an array of objects.
Each object has "category" (string, one of the fixed keys) and "content" (string).
If nothing is extractable, return {{"items": []}}.

Examples:

Input: "Hi, I am Priya"
Output: {{"items": [{{"category": "personal_info", "content": "Candidate's name is Priya"}}]}}

Input: "I am from Indore"
Output: {{"items": [{{"category": "location", "content": "Based in Indore"}}]}}

Input: "I completed my B.Com last year"
Output: {{"items": [{{"category": "education", "content": "Completed B.Com last year"}}]}}

Input: "I worked at a call center for three months"
Output: {{"items": [{{"category": "work_experience", "content": "Worked at a call center for 3 months"}}]}}

Input: "I want to work in customer support, I like talking to people"
Output: {{"items": [{{"category": "job_interest", "content": "Interested in customer support roles"}}, {{"category": "preference", "content": "Enjoys talking to people"}}]}}

Input: "I know Excel and have good typing speed"
Output: {{"items": [{{"category": "skills", "content": "Knows Excel"}}, {{"category": "skills", "content": "Has good typing speed"}}]}}

Input: "Hello"
Output: {{"items": []}}

Input: "Yes"
Output: {{"items": []}}

Input: "I am a bit nervous today"
Output: {{"items": [{{"category": "personality", "content": "Feeling nervous during the call"}}]}}"""


async def ensure_user_entity(
    memory_client: AsyncMemoryClient,
    user_id: str,
) -> bool:
    try:
        users_api = await memory_client.users
        if callable(users_api):
            users_api = users_api()
    except Exception as e:
        logger.debug(f"Failed to get users API: {e}")
        return False

    try:
        existing = await users_api.get(user_id)
        if existing:
            logger.debug(f"User entity already exists: {user_id}")
            return False
    except Exception:
        pass

    try:
        await users_api.add(user_id=user_id)
        logger.info(f"Created user entity in mem0: {user_id}")
        return True
    except Exception as e:
        logger.debug(f"User entity creation returned: {e}")
        return False


async def search_memories(
    memory_client: AsyncMemoryClient,
    query: str,
    user_id: str,
    categories: Sequence[str] | None = None,
) -> dict[str, Any]:
    filters: dict[str, Any] = {"user_id": user_id}
    if categories:
        filters["metadata"] = {"category": {"in": categories}}
    return await memory_client.search(query, filters=filters)


async def extract_and_store_memory(
    memory_client: AsyncMemoryClient,
    user_id: str,
    user_text: str,
    history: list[dict[str, str]],
) -> None:
    try:
        global _openai_client
        if _openai_client is None:
            _openai_client = AsyncOpenAI(
                api_key=os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY"),
                base_url=os.getenv("OPENROUTER_BASE_URL", DEFAULT_OPENROUTER_BASE_URL),
            )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": MEMORY_EXTRACTION_PROMPT}
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": user_text})

        response = await _openai_client.chat.completions.create(
            model=DEFAULT_MEMORY_MODEL,
            messages=cast(list, messages),
            response_format={"type": "json_object"},
            max_completion_tokens=500,
        )

        try:
            parsed = json.loads(response.choices[0].message.content or "{}")
            items = parsed.get("items", [])
        except json.JSONDecodeError:
            items = []

        if not items:
            logger.debug("No extractable memories from user message")
            return

        for item in items:
            if not isinstance(item, dict):
                continue

            category = item.get("category", "")
            content = item.get("content", "")

            if not isinstance(content, str) or not content.strip():
                continue

            if category not in VALID_CATEGORIES:
                logger.warning(
                    f"LLM returned unknown category '{category}', skipping: {content[:50]}"
                )
                continue

            await memory_client.add(
                [{"role": "user", "content": content.strip()}],
                user_id=user_id,
                metadata={"category": category},
                infer=False,
            )
            logger.info(f"Stored memory [{category}]: {content[:50]}...")
    except Exception as e:
        logger.warning(f"Failed to extract/store memory: {e}")


__all__ = [
    "AsyncMemoryClient",
    "MemoryCategory",
    "ensure_user_entity",
    "extract_and_store_memory",
    "search_memories",
]
