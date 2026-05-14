from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum

from livekit.agents import AgentSession
from livekit.plugins import openai
from openai.types.beta.realtime.session import TurnDetection

logger = logging.getLogger(__name__)

DEFAULT_REALTIME_MODEL = "gpt-realtime-2"
DEFAULT_REALTIME_VOICE = "marin"

_VALID_OPENAI_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "coral",
    "echo",
    "sage",
    "shimmer",
    "verse",
    "marin",
    "cedar",
}


class InteractionMode(str, Enum):
    AUTO = "auto"
    PTT = "ptt"


@dataclass(frozen=True)
class SessionConfig:
    voice: str | None = None


def build_agent_session(
    *,
    voice: str = DEFAULT_REALTIME_VOICE,
    mode: InteractionMode = InteractionMode.AUTO,
    session_config: SessionConfig | None = None,
) -> AgentSession:
    effective_session_config = session_config or SessionConfig()
    effective_voice = effective_session_config.voice or voice

    if effective_voice not in _VALID_OPENAI_VOICES:
        logger.warning(
            f"Invalid Realtime voice '{effective_voice}'. "
            f"Falling back to '{DEFAULT_REALTIME_VOICE}'. "
            f"Valid voices: {', '.join(sorted(_VALID_OPENAI_VOICES))}"
        )
        effective_voice = DEFAULT_REALTIME_VOICE

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is required for the Realtime API. "
            "Get your key at https://platform.openai.com/api-keys"
        )

    llm = openai.realtime.RealtimeModel(
        model=DEFAULT_REALTIME_MODEL,
        voice=effective_voice,
        turn_detection=TurnDetection(
            type="semantic_vad",
            eagerness="medium",
            create_response=True,
            interrupt_response=True,
        ),
    )

    common_kwargs = {
        "llm": llm,
        "allow_interruptions": True,
        "min_interruption_duration": 0.5,
        "min_endpointing_delay": 0.5,
        "max_endpointing_delay": 3.0,
        "min_consecutive_speech_delay": 0.2,
    }

    if mode is InteractionMode.PTT:
        return AgentSession(
            **common_kwargs,
            turn_detection="manual",
            resume_false_interruption=True,
            use_tts_aligned_transcript=True,
            preemptive_generation=False,
        )

    return AgentSession(**common_kwargs)
