from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

APP_DIR = Path(__file__).resolve().parent.parent

load_dotenv(str(APP_DIR / ".env.local"))
load_dotenv(str(APP_DIR / ".env"))

PROMPT_PATH = APP_DIR / "PROMPT.md"
AGENT_NAME = "job-finder-agent"
DEFAULT_PROMPT_AGENT_NAME = "Sara"
DEFAULT_PROMPT_USER_NAME = "the candidate"
DEFAULT_OPENROUTER_MODEL = "openai/gpt-5.1"
DEFAULT_SARVAM_TTS_LANGUAGE = "en-IN"
DEFAULT_SARVAM_TTS_MODEL = "bulbul:v3"
DEFAULT_SARVAM_TTS_SPEAKER = "ritu"
DEFAULT_SARVAM_TTS_DICT_ID = "p_fcfdd23b"

INITIAL_REPLY = (
    "Greet the user, introduce yourself as their job search agent, and ask "
    "what kind of role they are trying to land."
)
CALLER_LOOKUP_TIMEOUT_SECONDS = 5

REGISTERED_AGENT_NAME = os.getenv("AGENT_NAME", AGENT_NAME)
