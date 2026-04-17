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

## Room Metadata

Use room metadata to control turn behavior and inject prompt variables per session.

```json
{
  "interaction_mode": "ptt",
  "prompt_context": {
    "agentName": "Maya",
    "userName": "Ravi",
    "jobRole": "Data Analyst",
    "companyName": "Zoho",
    "prompt": "Keep the tone slightly more formal."
  }
}
```

Rules:

- `interaction_mode` supports only `auto` and `ptt`
- `prompt_context` is a flat object of prompt placeholders
- built-in defaults always exist for `{agentName}` and `{userName}`
- the default `{agentName}` value is configured in `src/constants.py`
- any extra `prompt_context` keys can be referenced directly in `PROMPT.md` as `{keyName}`

## Recording Toggle

Set `ENABLE_RECORDING=false` to disable the S3 egress recording flow even when the S3 environment variables are present.

When recording is enabled, the agent stores audio and transcript in S3. After the egress completes successfully and the transcript upload finishes, it sends a JSON `POST` request to `WEBHOOK_URL`.

## Deploy

```bash
lk agent create
```
