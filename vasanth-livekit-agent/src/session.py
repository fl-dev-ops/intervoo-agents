from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

from livekit.agents import (
    AgentSession,
    PreemptiveGenerationOptions,
    TurnHandlingOptions,
)
from livekit.agents.inference import TurnDetector
from livekit.plugins import (
    assemblyai,  # noqa: F401 - retained as an STT fallback
    deepgram,
    openai,
    sarvam,
)

from qwen_tts import QwenTTS

# ---------------------------------------------------------------------------
# Sarvam TTS pool patch (workaround for livekit/agents#5681)
# The pool defaults to max_session_duration=3600 but Sarvam closes idle WS
# connections after ~60s. Setting it to 50s forces proactive recycling.
# Remove once the fix is upstreamed into livekit-plugins-sarvam.
# ---------------------------------------------------------------------------
_SARVAM_POOL_MAX_SESSION_DURATION = 50.0  # seconds, below Sarvam's 60s idle timeout

logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.5"
DEFAULT_DEEPGRAM_STT_MODEL = "flux-general-en"
DEFAULT_SARVAM_LANGUAGE = "en-IN"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3"
DEFAULT_ASSEMBLYAI_STT_MODEL = "universal-3-5-pro"


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required when TTS_PROVIDER=qwen")
    return value


def _build_tts(
    *,
    tts_speaker: str,
    tts_dict_id: str | None,
    tts_model: str,
    session_config: SessionConfig,
) -> sarvam.TTS | QwenTTS:
    provider = os.getenv("TTS_PROVIDER", "sarvam").strip().lower()
    if provider == "qwen":
        return _build_qwen_tts()
    if provider != "sarvam":
        raise ValueError("TTS_PROVIDER must be either 'sarvam' or 'qwen'")

    tts = sarvam.TTS(
        target_language_code=DEFAULT_SARVAM_LANGUAGE,
        model=tts_model,
        speaker=session_config.voice or tts_speaker,
        pace=session_config.speaking_speed or 1.0,
        temperature=0.6,
        enable_preprocessing=True,
        output_audio_bitrate="128k",
        min_buffer_size=50,
        max_chunk_length=150,
        dict_id=session_config.dict_id or tts_dict_id,
    )
    if hasattr(tts, "prewarm"):
        tts.prewarm()
    if hasattr(tts, "_pool"):
        tts._pool._max_session_duration = _SARVAM_POOL_MAX_SESSION_DURATION
        tts._pool._mark_refreshed_on_get = True
    return tts


def _build_qwen_tts() -> QwenTTS:
    return QwenTTS(
        endpoint=_required_env("QWEN_TTS_ENDPOINT"),
        voice=_required_env("QWEN_TTS_VOICE"),
        model_label=os.getenv("QWEN_TTS_MODEL_LABEL", "vasanth-best").strip()
        or "vasanth-best",
        language=os.getenv("QWEN_TTS_LANGUAGE", "English").strip() or "English",
        api_key=_required_env("QWEN_TTS_API_KEY"),
        connect_timeout=float(os.getenv("QWEN_TTS_CONNECT_TIMEOUT_SECONDS", "10")),
        total_timeout=float(os.getenv("QWEN_TTS_TOTAL_TIMEOUT_SECONDS", "120")),
        max_retries=int(os.getenv("QWEN_TTS_MAX_RETRIES", "1")),
        retry_interval=float(os.getenv("QWEN_TTS_RETRY_INTERVAL_SECONDS", "1")),
    )


def validate_tts_provider_configuration() -> None:
    if os.getenv("TTS_PROVIDER", "sarvam").strip().lower() == "qwen":
        _build_qwen_tts()


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
    stt = deepgram.STTv2(
        model=DEFAULT_DEEPGRAM_STT_MODEL,
    )
    # stt = sarvam.STT(
    #     language=DEFAULT_SARVAM_LANGUAGE,
    #     model="saaras:v3",
    #     mode="transcribe",
    # )
    # stt = assemblyai.STT(
    #     model=DEFAULT_ASSEMBLYAI_STT_MODEL,
    #     language_codes=["en"],
    #     min_turn_silence=100,
    #     max_turn_silence=1000,
    #     vad_threshold=0.3,
    # )

    llm = openai.LLM.with_openrouter(model=openrouter_model)

    effective_session_config = session_config or SessionConfig()
    tts = _build_tts(
        tts_speaker=tts_speaker,
        tts_dict_id=tts_dict_id,
        tts_model=tts_model,
        session_config=effective_session_config,
    )

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
        # start_question speculatively, causing question-jumping.
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
                "max_delay": 1.5,
            },
            interruption={
                "min_duration": 0.5,
                "resume_false_interruption": True,
            },
            preemptive_generation=preemptive_generation,
            user_turn_limit={
                "max_duration": 60.0,
            },
        ),
    )
