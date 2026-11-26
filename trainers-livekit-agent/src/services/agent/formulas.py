"""Formulas to the chat (Prathosh first; on for profiles with "formulas_to_chat": true).

The model writes a formula as [[ latex || words ]]. This filter sits on the LLM output, before the voice and the
transcript: everything downstream (TTS, captions, chat history) gets only the words, and the LaTeX goes to the
learner's browser on its own data topic, where the chat typesets it in place of those words. The LiveKit transcript
path rewrites markdown-like text (it dropped brackets and underscores in a live test), so the LaTeX never goes
through it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterable, AsyncIterator, Callable
from typing import Any

from livekit.agents import llm

logger = logging.getLogger(__name__)

FORMULA_TOPIC = "twin-formula"
_OPEN, _SEP, _CLOSE = "[[", "||", "]]"
_MAX_PENDING = 600  # a marker that never closes is released as plain text after this many characters


class FormulaSplitter:
    """Streaming: feed text pieces, get back the text to speak; finished formulas go to `on_formula`."""

    def __init__(self, on_formula: Callable[[str, str], None]) -> None:
        self._on_formula = on_formula
        self._pending = ""

    def feed(self, piece: str) -> str:
        self._pending += piece
        out = []
        while True:
            start = self._pending.find(_OPEN)
            if start < 0:
                # keep a trailing "[" in case it is the first half of "[["
                keep = 1 if self._pending.endswith("[") else 0
                out.append(self._pending[: len(self._pending) - keep])
                self._pending = self._pending[len(self._pending) - keep :]
                break
            end = self._pending.find(_CLOSE, start + 2)
            if end < 0:
                out.append(self._pending[:start])
                self._pending = self._pending[start:]
                if len(self._pending) > _MAX_PENDING:
                    out.append(self._clean(self._pending))
                    self._pending = ""
                break
            out.append(self._pending[:start])
            out.append(self._resolve(self._pending[start + 2 : end]))
            self._pending = self._pending[end + 2 :]
        return "".join(out)

    def flush(self) -> str:
        rest, self._pending = self._pending, ""
        return self._clean(rest)

    def _resolve(self, inner: str) -> str:
        if _SEP not in inner:
            return inner.strip()
        latex, words = (part.strip() for part in inner.split(_SEP, 1))
        if latex and words:
            self._on_formula(latex, words)
        return words or latex

    @staticmethod
    def _clean(text: str) -> str:
        if _SEP in text:
            text = text.split(_SEP, 1)[1]
        return text.replace(_OPEN, "").replace(_CLOSE, "").replace(_SEP, "")


def room_publisher(room: Any) -> Callable[[str, str], None]:
    """Sends {id, latex, words} to everyone in the room on the formula topic, without blocking the reply."""

    def publish(latex: str, words: str) -> None:
        payload = json.dumps({"id": uuid.uuid4().hex[:12], "latex": latex, "words": words})

        async def send() -> None:
            try:
                await room.local_participant.publish_data(payload, reliable=True, topic=FORMULA_TOPIC)
                logger.info("formula_sent words=%r", words[:80])
            except Exception as e:  # the spoken words already went out; the chat just shows them as words
                logger.warning("formula_send_failed: %s", e)

        asyncio.get_running_loop().create_task(send())

    return publish


async def formulas_to_chat(
    chunks: AsyncIterable[Any], on_formula: Callable[[str, str], None]
) -> AsyncIterator[Any]:
    splitter = FormulaSplitter(on_formula)
    last_chunk: llm.ChatChunk | None = None
    async for chunk in chunks:
        if isinstance(chunk, str):
            text = splitter.feed(chunk)
            if text:
                yield text
            continue
        if isinstance(chunk, llm.ChatChunk) and chunk.delta is not None and chunk.delta.content:
            last_chunk = chunk
            text = splitter.feed(chunk.delta.content)
            if text or chunk.delta.tool_calls:
                yield chunk.model_copy(update={"delta": chunk.delta.model_copy(update={"content": text or None})})
            continue
        yield chunk
    rest = splitter.flush()
    if rest:
        if last_chunk is not None:
            yield last_chunk.model_copy(update={"delta": last_chunk.delta.model_copy(update={"content": rest, "tool_calls": []})})
        else:
            yield rest
