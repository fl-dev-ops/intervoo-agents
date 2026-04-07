# Job Finder Agent

Standalone LiveKit voice agent for job search coaching. This app is separate from the existing `agent` app and is intended to be deployed independently with the worker name `job-finder-agent`.

## Stack

- STT: Deepgram
- LLM: OpenAI plugin with OpenRouter
- TTS: Sarvam
- VAD: Silero
- Memory: Mem0 with structured extraction via OpenRouter

## Files

- `src/agent.py`: LiveKit worker entrypoint
- `src/memory.py`: Mem0 helpers copied from the existing Calypso agent setup
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
