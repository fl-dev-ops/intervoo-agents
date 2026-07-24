from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from livekit.agents import (
    AgentSession,
    PreemptiveGenerationOptions,
    TurnHandlingOptions,
)
from livekit.agents.inference import TurnDetector
from livekit.plugins import assemblyai, openai, sarvam

from qwen_tts import QwenTTS  # noqa: F401  # kept live for quick TTS engine toggle

# Custom Qwen voice-clone TTS endpoint ("vasanth-best" voice).
# NOTE: this is an ephemeral JarvisLabs notebook URL — update it here whenever
# the notebook restarts and its subdomain rotates.
QWEN_TTS_ENDPOINT = (
    "https://13c6184528451.notebooksc.jarvislabs.net/v1/audio/speech/streaming"
)

# ---------------------------------------------------------------------------
# Sarvam TTS pool patch (workaround for livekit/agents#5681)
# The pool defaults to max_session_duration=3600 but Sarvam closes idle WS
# connections after ~60s. Setting it to 50s forces proactive recycling.
# Remove once the fix is upstreamed into livekit-plugins-sarvam.
# ---------------------------------------------------------------------------
_SARVAM_POOL_MAX_SESSION_DURATION = 50.0  # seconds, below Sarvam's 60s idle timeout

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_MODEL = "openai/gpt-4o"
DEFAULT_SARVAM_LANGUAGE = "en-IN"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3"
DEFAULT_ASSEMBLYAI_STT_MODEL = "universal-3-5-pro"


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
    turn_detector: Any | None = None,
    disable_preemptive_generation: bool = False,
) -> AgentSession:
    # stt = sarvam.STT(
    #     language=DEFAULT_SARVAM_LANGUAGE,
    #     model="saaras:v3",
    #     mode="transcribe",
    # )
    stt = assemblyai.STT(
        model=DEFAULT_ASSEMBLYAI_STT_MODEL,
        language_codes=["en"],
        min_turn_silence=100,
        max_turn_silence=1000,
        vad_threshold=0.3,
    )

    llm = openai.LLM.with_openrouter(model=openrouter_model)

    # Sarvam TTS (active)
    effective_session_config = session_config or SessionConfig()
    # tts = sarvam.TTS(
    #     target_language_code=DEFAULT_SARVAM_LANGUAGE,
    #     model=tts_model,
    #     speaker=effective_session_config.voice or tts_speaker,
    #     pace=effective_session_config.speaking_speed or 1.0,
    #     temperature=0.6,
    #     enable_preprocessing=True,
    #     output_audio_bitrate="128k",
    #     min_buffer_size=50,
    #     max_chunk_length=150,
    #     dict_id=effective_session_config.dict_id or tts_dict_id,
    # )
    # if hasattr(tts, "prewarm"):
    #     tts.prewarm()
    # # Patch the connection pool so it recycles connections before Sarvam's
    # # server-side 60s idle timeout evicts them (livekit/agents#5681).
    # if hasattr(tts, "_pool"):
    #     tts._pool._max_session_duration = _SARVAM_POOL_MAX_SESSION_DURATION
    #     tts._pool._mark_refreshed_on_get = True

    tts = QwenTTS(QWEN_TTS_ENDPOINT)
    # -----------------------------------------------------------------------

    if mode is InteractionMode.PTT:
        return AgentSession(
            stt=stt,
            llm=llm,
            tts=tts,
            turn_handling=TurnHandlingOptions(
                turn_detection="manual",
                interruption={
                    "min_duration": 0.5,
                    "resume_false_interruption": True,
                },
            ),
            use_tts_aligned_transcript=True,
            preemptive_generation=False,
        )

    preemptive_generation: PreemptiveGenerationOptions = (
        # Structured interviews (e.g. the diagnostic agent) must act only on a
        # completed turn. Preemptive generation runs the LLM on partial
        # transcripts and fires screen-publishing tools like
        # mark_question_started speculatively, causing question-jumping.
        {"enabled": False}
        if disable_preemptive_generation
        else {}
    )

    return AgentSession(
        stt=stt,
        llm=llm,
        tts=tts,
        turn_handling=TurnHandlingOptions(
            turn_detection=turn_detector or TurnDetector(version="v1"),
            endpointing={
                "mode": "dynamic",
                "min_delay": 0.5,
                "max_delay": 2.0,
            },
            interruption={
                "min_duration": 0.5,
                "resume_false_interruption": True,
            },
            preemptive_generation=preemptive_generation,
        ),
    )
