# Interview Coaching Agent

Standalone LiveKit voice agent for interview coaching. This app is separate from the existing `agent` app and is intended to be deployed independently with the worker name `interview-coaching-agent`.

## Stack

- STT: Deepgram
- LLM: OpenAI plugin with OpenRouter
- TTS: Sarvam
- VAD: Silero

## Files

- `src/agent.py`: LiveKit worker entrypoint
- `PROMPT.md`: system prompt loaded at runtime
- `tests/test_agent.py`: unit tests plus an optional integration test

## Setup

```bash
cp .env.example .env.local
uv sync --group dev
uv run src/agent.py download-files
```

## Run

```bash
uv run src/agent.py dev
```

Production mode:

```bash
uv run src/agent.py start
```

## Test

```bash
uv sync --group dev
uv run python -m pytest -q
```

## Deploy

```bash
lk agent create
```
