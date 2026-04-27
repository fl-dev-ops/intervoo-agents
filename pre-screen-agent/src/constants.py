from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent

load_dotenv(str(APP_DIR / ".env.local"))
load_dotenv(str(APP_DIR / ".env"))

PROMPT_PATH = APP_DIR / "PROMPT-v2.md"
AGENT_NAME = "pre-screen-agent"
DEFAULT_PROMPT_AGENT_NAME = "Sara"
DEFAULT_PROMPT_USER_NAME = "the student"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.1"
DEFAULT_SARVAM_TTS_LANGUAGE = "en-IN"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3"
DEFAULT_SARVAM_TTS_SPEAKER = "ishita"
DEFAULT_SARVAM_TTS_DICT_ID = "p_fcfdd23b"

INITIAL_REPLY = (
    "Greet the user, introduce yourself as their interview coach, and ask "
    "what role or interview they want to prepare for today."
)
CALLER_LOOKUP_TIMEOUT_SECONDS = 5

END_CALL_EXTRA_DESCRIPTION = (
    "Only end the call when the user clearly indicates the conversation is complete."
)
END_CALL_INSTRUCTIONS = "Thanks for practicing with me today. Goodbye."

REGISTERED_AGENT_NAME = os.getenv("AGENT_NAME", AGENT_NAME)
