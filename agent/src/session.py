from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from livekit.agents import AgentSession, TurnHandlingOptions
from livekit.plugins import openai, sarvam, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.1"
DEFAULT_SARVAM_LANGUAGE = "en-IN"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3"


class InteractionMode(str, Enum):
    AUTO = "auto"
    PTT = "ptt"


@dataclass(frozen=True)
class SessionConfig:
    voice: str | None = None
    speaking_speed: float | None = None
    dict_id: str | None = None


def build_agent_session(
    *,
    openrouter_model: str = DEFAULT_OPENROUTER_MODEL,
    tts_speaker: str,
    tts_dict_id: str | None,
    tts_model: str = DEFAULT_SARVAM_TTS_MODEL,
    mode: InteractionMode = InteractionMode.AUTO,
    session_config: SessionConfig | None = None,
    vad: Any | None = None,
    turn_detector: Any | None = None,
) -> AgentSession:
    effective_session_config = session_config or SessionConfig()

    stt = sarvam.STT(
        language=DEFAULT_SARVAM_LANGUAGE,
        model="saaras:v3",
        mode="transcribe",
    )

    llm = openai.LLM.with_openrouter(model=openrouter_model)

    tts = sarvam.TTS(
        target_language_code=DEFAULT_SARVAM_LANGUAGE,
        model=tts_model,
        speaker=effective_session_config.voice or tts_speaker,
        pace=effective_session_config.speaking_speed or 1.0,
        temperature=0.6,
        enable_preprocessing=True,
        output_audio_bitrate="128k",
        min_buffer_size=50,
        max_chunk_length=150,
        dict_id=effective_session_config.dict_id or tts_dict_id,
    )

    if hasattr(tts, "prewarm"):
        tts.prewarm()

    if mode is InteractionMode.PTT:
        return AgentSession(
            stt=stt,
            llm=llm,
            tts=tts,
            turn_handling=TurnHandlingOptions(
                turn_detection="manual",
                interruption={
                    "mode": "adaptive",
                    "min_duration": 0.5,
                    "resume_false_interruption": True,
                },
            ),
            use_tts_aligned_transcript=True,
            preemptive_generation=False,
        )

    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        vad=vad or silero.VAD.load(),
        turn_handling=TurnHandlingOptions(
            turn_detection=turn_detector or MultilingualModel(),
            endpointing={
                "mode": "dynamic",
                "min_delay": 3.0,
                "max_delay": 6.0,
            },
            interruption={
                "mode": "adaptive",
                "min_duration": 0.5,
                "resume_false_interruption": True,
            },
        ),
    )
