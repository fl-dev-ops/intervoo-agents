# LiveKit Voice Agent Template

Standalone Python template for building a LiveKit voice agent with configurable prompt context, optional push-to-talk mode, and optional recording webhooks.

## Stack

- Runtime: Python 3.10+
- Agent framework: LiveKit Agents
- STT: Sarvam STT
- LLM: OpenAI plugin with OpenRouter
- TTS: Sarvam TTS
- VAD: Silero

## Project Structure

- `src/agent.py`: LiveKit worker entrypoint
- `src/constants.py`: runtime defaults and environment-backed settings
- `src/prompt.py`: prompt loading and prompt-context rendering
- `src/recording.py`: optional S3 recording, transcript upload, and webhook delivery
- `PROMPT.md`: system prompt template loaded at runtime
- `tests/`: unit tests

## Setup

```bash
cp .env.example .env.local
uv sync --group dev
```

If your plugins require model assets, download them before local development:

```bash
uv run src/agent.py download-files
```

## Run

Development:

```bash
uv run src/agent.py dev
```

Production:

```bash
uv run src/agent.py start
```

## Test

```bash
uv sync --group dev
uv run python -m pytest -q
```

## Prompt Context

The agent reads room metadata and injects values into `PROMPT.md`.

Example room metadata:

```json
{
  "interaction_mode": "ptt",
  "prompt_context": {
    "agentName": "Maya",
    "userName": "Ravi",
    "jobRole": "Data Analyst",
    "companyName": "Zoho",
    "additionalNote": "Keep the tone slightly more formal."
  },
  "config": {
    "voice": "kavya",
    "speakingSpeed": 0.9
  }
}
```

Rules:

- `interaction_mode` supports `auto` and `ptt`
- `prompt_context` is a flat object of prompt placeholders
- built-in defaults always exist for `{agentName}` and `{userName}`
- extra `prompt_context` keys can be referenced directly in `PROMPT.md` as `{keyName}`
- `config.voice` overrides the default TTS speaker for the session
- `config.speakingSpeed` overrides the default TTS pace for the session

## Recording And Webhooks

Recording is optional.

- Set `ENABLE_RECORDING=false` to disable recording entirely
- When enabled, the agent stores audio and transcript in S3
- After egress completes and transcript upload succeeds, the agent sends a JSON `POST` request to `WEBHOOK_URL`

The webhook payload includes:

- agent metadata
- room and job identifiers
- egress status
- audio and transcript URLs
- participant identity details
- normalized transcript data

## Environment Variables

See `.env.example` for the full list. Common variables:

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `AGENT_NAME`
- `OPENROUTER_API_KEY`
- `OPENROUTER_MODEL`
- `SARVAM_API_KEY`
- `ENABLE_RECORDING`
- `WEBHOOK_URL`

## Template Notes

- Update `PROMPT.md` for your use case
- Update `src/constants.py` if you want different defaults
- Replace any project-specific values in `livekit.toml` before publishing or deploying

## Deploy

```bash
lk agent create
```
