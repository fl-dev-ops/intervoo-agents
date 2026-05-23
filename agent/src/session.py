from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from livekit.agents import AgentSession
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
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            tts.prewarm()

    common_kwargs = {
        "stt": stt,
        "llm": llm,
        "tts": tts,
        "allow_interruptions": True,
        "min_interruption_duration": 0.5,
        "min_endpointing_delay": 1.5,
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

    return AgentSession(
        **common_kwargs,
        vad=silero.VAD.load(),
        turn_detection=MultilingualModel(),
    )
