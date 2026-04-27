from __future__ import annotations

LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "english": {"label": "English", "stt_language": "en-IN", "tts_language": "en-IN"},
    "hindi": {"label": "Hindi", "stt_language": "hi-IN", "tts_language": "hi-IN"},
    "tamil": {"label": "Tamil", "stt_language": "ta-IN", "tts_language": "ta-IN"},
}

LANGUAGE_ALIASES: dict[str, str] = {
    "en": "english",
    "en-in": "english",
    "english": "english",
    "hi": "hindi",
    "hi-in": "hindi",
    "hindi": "hindi",
    "hindui": "hindi",
    "ta": "tamil",
    "ta-in": "tamil",
    "tamil": "tamil",
}

CODEMIX_STT_LANGUAGES: set[str] = {"hi-IN", "ta-IN"}


def resolve_language_config(language: str | None) -> dict[str, str]:
    if not language:
        return LANGUAGE_CONFIGS["english"]
    normalized = LANGUAGE_ALIASES.get(language.strip().lower(), "english")
    return LANGUAGE_CONFIGS[normalized]


def resolve_stt_mode(stt_language: str) -> str:
    return "codemix" if stt_language in CODEMIX_STT_LANGUAGES else "transcribe"
