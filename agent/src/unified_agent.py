from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from livekit.agents import Agent, ChatContext, ChatMessage, RunContext, function_tool

from identity import resolve_user_id_from_call_context
from memory import (
    AsyncMemoryClient,
    MemoryCategory,
    ensure_user_entity,
    extract_and_store_memory,
    search_memories,
)

logger = logging.getLogger(__name__)

MIN_WORDS_FOR_RETRIEVAL = 3
SESSION_TIMER_INTERVAL_SECONDS = 60
SESSION_TIMER_ROLE = "developer"


class UnifiedAgent(Agent):
    def __init__(
        self,
        *,
        instructions: str,
        tools: list[Any],
        initial_reply: str,
        memory_client: AsyncMemoryClient | None = None,
        user_id: str | None = None,
        participant_identity: str | None = None,
        participant_attributes: dict[str, str] | None = None,
        room_name: str | None = None,
    ) -> None:
        self.initial_reply = initial_reply
        self.memory_client = memory_client
        self.user_id = user_id
        self.participant_identity = participant_identity
        self.participant_attributes = participant_attributes
        self.room_name = room_name
        self._memory_tasks: set[asyncio.Task[None]] = set()
        self._session_timer_task: asyncio.Task[None] | None = None

        effective_tools = list(tools)
        if memory_client is not None:
            effective_tools.append(self.recall_memory)

        super().__init__(instructions=instructions, tools=effective_tools)

    def _build_elapsed_time_context(self, elapsed_minutes: int) -> str:
        return (
            f"[Internal timing context: {elapsed_minutes} minute"
            f"{'s' if elapsed_minutes != 1 else ''} elapsed since this session started. "
            "Use this only for pacing. Do not mention this timing message to the candidate "
            "unless explicitly relevant.]"
        )

    async def _inject_elapsed_time_context(self, elapsed_minutes: int) -> None:
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.add_message(
            role=SESSION_TIMER_ROLE,
            content=self._build_elapsed_time_context(elapsed_minutes),
            extra={
                "internal_timer": True,
                "elapsed_minutes": elapsed_minutes,
            },
        )
        await self.update_chat_ctx(chat_ctx)
        logger.info(
            "Injected session timing context: elapsed_minutes=%s, role=%s",
            elapsed_minutes,
            SESSION_TIMER_ROLE,
        )

    async def _run_session_timer(self) -> None:
        elapsed_minutes = 0
        while True:
            await asyncio.sleep(SESSION_TIMER_INTERVAL_SECONDS)
            elapsed_minutes += 1
            try:
                await self._inject_elapsed_time_context(elapsed_minutes)
            except Exception as e:
                logger.warning(f"Failed to inject session timing context: {e}")

    def _start_session_timer(self) -> None:
        if self._session_timer_task is not None and not self._session_timer_task.done():
            return
        self._session_timer_task = asyncio.create_task(
            self._run_session_timer(),
            name=f"session-timer:{self.room_name or self.participant_identity or 'agent'}",
        )

    async def _stop_session_timer(self) -> None:
        if self._session_timer_task is None:
            return
        self._session_timer_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._session_timer_task
        self._session_timer_task = None

    @function_tool
    async def recall_memory(
        self,
        context: RunContext,
        query: str,
    ) -> str:
        """Search past conversations for relevant information about the user."""
        if self.memory_client is None or self.user_id is None:
            return "Memory system not available."
        try:
            results = await search_memories(self.memory_client, query, self.user_id)
            if results.get("results"):
                parts = [r["memory"] for r in results["results"]]
                return "Relevant memories:\n" + "\n\n".join(parts)
            return "No relevant memories found."
        except Exception as e:
            logger.warning(f"Failed to recall memory: {e}")
            return "I couldn't retrieve any memories at the moment."

    def _build_recent_history(self, chat_ctx: ChatContext) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        recent_messages = list(chat_ctx.messages())[-5:]
        for msg in recent_messages:
            if isinstance(msg.content, list):
                text_content = ""
                for item in msg.content:
                    if isinstance(item, str):
                        text_content += item
                if text_content:
                    history.append({"role": msg.role, "content": text_content})
            elif isinstance(msg.content, str):
                history.append({"role": msg.role, "content": msg.content})
        return history

    async def on_enter(self) -> None:
        self._start_session_timer()

        if self.memory_client is None:
            await self.session.generate_reply(instructions=self.initial_reply)
            return

        participant_identity = self.participant_identity
        participant_attributes = self.participant_attributes
        room_name = self.room_name

        if participant_identity is None or room_name is None:
            try:
                room_io_instance = self.session.room_io
            except RuntimeError:
                room_io_instance = None

            linked = getattr(room_io_instance, "linked_participant", None)
            if participant_identity is None:
                participant_identity = linked.identity if linked else None
            if participant_attributes is None:
                participant_attributes = (
                    dict(linked.attributes.items())
                    if linked and linked.attributes
                    else None
                )
            if room_name is None:
                room_name = (
                    room_io_instance.room.name
                    if room_io_instance is not None
                    and room_io_instance.room is not None
                    else None
                )

        if self.user_id is not None:
            self.user_id = resolve_user_id_from_call_context(
                current_user_id=self.user_id,
                participant_identity=participant_identity,
                participant_attributes=participant_attributes,
                room_name=room_name,
            )
        logger.info(f"Using memory user_id: {self.user_id}")

        if self.user_id is not None:
            try:
                await ensure_user_entity(self.memory_client, self.user_id)
            except Exception as e:
                logger.warning(f"Failed to ensure user entity: {e}")

        greeting_categories = [
            MemoryCategory.PERSONAL_INFO,
            MemoryCategory.LOCATION,
            MemoryCategory.EDUCATION,
            MemoryCategory.WORK_EXPERIENCE,
            MemoryCategory.JOB_INTEREST,
            MemoryCategory.PREFERENCE,
            MemoryCategory.SCREENING_RESULT,
            MemoryCategory.PERSONALITY,
        ]

        context_summary = ""
        try:
            if self.user_id is not None:
                results = await search_memories(
                    self.memory_client,
                    "candidate background preferences past conversations",
                    self.user_id,
                    categories=greeting_categories,
                )
                if results.get("results"):
                    parts = [r["memory"] for r in results["results"]]
                    context_summary = "\n".join(parts)
                    logger.info(f"Loaded {len(parts)} memories for user context")
        except Exception as e:
            logger.warning(f"Failed to load user context: {e}")

        if context_summary:
            await self.session.generate_reply(
                instructions=(
                    "Here is what you know about this user from past conversations:\n"
                    f"{context_summary}\n\n"
                    "This is the first utterance of a new phone call. "
                    "Start with a neutral greeting. Do not imply you are continuing "
                    "an earlier sentence. Greet the user warmly by name if known, "
                    "optionally mention one remembered preference naturally, and "
                    "follow your initial reply guidance: "
                    f"{self.initial_reply}"
                ),
            )
        else:
            await self.session.generate_reply(instructions=self.initial_reply)

    async def on_exit(self) -> None:
        await self._stop_session_timer()
        await super().on_exit()

    async def on_user_turn_completed(
        self,
        turn_ctx: ChatContext,
        new_message: ChatMessage,
    ) -> None:
        if self.memory_client is None or self.user_id is None:
            await super().on_user_turn_completed(turn_ctx, new_message)
            return

        content = new_message.content
        user_text = ""
        if isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    user_text = item
                    break
        elif isinstance(content, str):
            user_text = content

        if not user_text:
            await super().on_user_turn_completed(turn_ctx, new_message)
            return

        word_count = len(user_text.split())
        should_retrieve = word_count >= MIN_WORDS_FOR_RETRIEVAL

        history = self._build_recent_history(turn_ctx)
        memory_task = asyncio.create_task(
            extract_and_store_memory(
                self.memory_client,
                self.user_id,
                user_text,
                history,
            )
        )
        self._memory_tasks.add(memory_task)
        memory_task.add_done_callback(self._memory_tasks.discard)

        if not should_retrieve:
            logger.debug(f"Skipping retrieval for short message ({word_count} words)")
            await super().on_user_turn_completed(turn_ctx, new_message)
            return

        try:
            search_results = await search_memories(
                self.memory_client,
                user_text,
                self.user_id,
            )
            if search_results.get("results"):
                context_parts = []
                for result in search_results.get("results", []):
                    memory = result.get("memory", "")
                    role = result.get("role", "unknown")
                    score = result.get("score", 0)
                    if memory:
                        context_parts.append(
                            f"[{role} memory, similarity: {score:.2f}]: {memory}"
                        )

                if context_parts:
                    full_context = "\n\n".join(context_parts)
                    inject_content = (
                        f"Relevant memories from conversation:\n{full_context}"
                    )
                    turn_ctx.add_message(role="assistant", content=inject_content)
                    logger.info(
                        f"Injected RAG context with {len(context_parts)} memories"
                    )
        except Exception as e:
            logger.warning(f"Failed to retrieve RAG context: {e}")

        await super().on_user_turn_completed(turn_ctx, new_message)
