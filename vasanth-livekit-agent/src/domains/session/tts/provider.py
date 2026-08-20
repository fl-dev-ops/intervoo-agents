"""TTS provider configuration and selection."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_SARVAM_LANGUAGE = "en-IN"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3"

_SARVAM_POOL_MAX_SESSION_DURATION = 50.0


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required when TTS_PROVIDER=qwen")
    return value


def build_sarvam_tts(
    *,
    tts_speaker: str,
    tts_dict_id: str | None,
    tts_model: str,
    session_config: Any,
) -> Any:
    from livekit.plugins import sarvam

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


def build_qwen_tts() -> Any:
    from interfaces.tts.qwen import QwenTTS

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


def build_tts(
    *,
    tts_speaker: str,
    tts_dict_id: str | None,
    tts_model: str,
    session_config: Any,
) -> Any:
    provider = os.getenv("TTS_PROVIDER", "sarvam").strip().lower()
    if provider == "qwen":
        return build_qwen_tts()
    if provider != "sarvam":
        raise ValueError("TTS_PROVIDER must be either 'sarvam' or 'qwen'")

    return build_sarvam_tts(
        tts_speaker=tts_speaker,
        tts_dict_id=tts_dict_id,
        tts_model=tts_model,
        session_config=session_config,
    )


def validate_tts_provider_configuration() -> None:
    if os.getenv("TTS_PROVIDER", "sarvam").strip().lower() == "qwen":
        build_qwen_tts()
