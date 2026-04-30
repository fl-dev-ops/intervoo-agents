from __future__ import annotations

import re

LANGUAGE_CONFIGS: dict[str, dict[str, str]] = {
    "english": {
        "label": "English",
        "stt_language": "en-IN",
        "tts_language": "en-IN",
        "language_style": "English",
        "greeting_style": "Warm English",
    },
    "hindi": {
        "label": "Hindi",
        "stt_language": "hi-IN",
        "tts_language": "hi-IN",
        "language_style": "Hinglish",
        "greeting_style": "Hindi-English",
    },
    "tamil": {
        "label": "Tamil",
        "stt_language": "ta-IN",
        "tts_language": "ta-IN",
        "language_style": "Tanglish",
        "greeting_style": "Tamil-English",
    },
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

ENGLISH_MARKERS: set[str] = {
    "i", "me", "my", "mine", "you", "your", "yours", "we", "our", "ours", "they",
    "the", "a", "an", "and", "or", "but", "if", "so", "because", "is", "are", "was",
    "were", "am", "do", "does", "did", "to", "for", "in", "on", "with", "of", "that",
    "this", "it", "what", "which", "how", "when", "where", "why", "yes", "no", "plan",
    "planning", "roles", "role", "would", "like", "try", "some", "these", "skills",
}

TAMIL_MARKERS: set[str] = {
    "naan", "neenga", "unga", "ungaluku", "ungalukku", "enna", "edhu", "iruku", "irukku",
    "iruka", "irukka", "romba", "konjam", "pathi", "pudikum", "pidikkum", "maari", "vandhu",
    "vanakkam", "seri", "appo", "inga", "nalla", "ennaoda", "ungaloda",
    "illa", "illai", "andha", "antha", "maadhiri", "madhiri", "edhuvum", "ethuvum",
    "pola", "ellam", "ellaam", "appadi", "ippadi", "yosikkala", "yosikala",
}

HINDI_MARKERS: set[str] = {
    "namaste", "main", "aap", "ap", "kya", "kaise", "mujhe", "mujh", "mera", "meri", "mere",
    "karna", "karni", "karunga", "karungi", "hai", "hain", "nahi", "haan", "acha", "accha",
    "thoda", "thodi", "aur", "kyunki", "lekin", "samajh", "bolo", "batao",
}

TAMIL_SCRIPT_RE = re.compile(r"[\u0B80-\u0BFF]")
DEVANAGARI_RE = re.compile(r"[\u0900-\u097F]")
LATIN_TOKEN_RE = re.compile(r"[a-zA-Z']+")


def resolve_language_config(language: str | None) -> dict[str, str]:
    if not language:
        return LANGUAGE_CONFIGS["english"]
    normalized = LANGUAGE_ALIASES.get(language.strip().lower(), "english")
    return LANGUAGE_CONFIGS[normalized]


def resolve_stt_mode(stt_language: str) -> str:
    return "codemix" if stt_language in CODEMIX_STT_LANGUAGES else "transcribe"
