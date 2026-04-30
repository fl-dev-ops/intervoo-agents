# CS Diagnostic Agent

Standalone LiveKit voice agent for Computer Science diagnostic assessment. Conducts structured technical interviews across five states: Welcome, Opening, Domain, Behavioral, and Closing.

## Stack

- Runtime: Python 3.10+
- Agent framework: LiveKit Agents
- STT: Sarvam STT (saaras:v3) / Deepgram (nova-3)
- LLM: OpenAI plugin with OpenRouter
- TTS: Sarvam TTS (bulbul:v3)
- VAD: Silero
- Knowledge base: Chroma Cloud
- Observability: Langfuse

## Project Structure

- `src/agent.py`: LiveKit worker entrypoint
- `src/constants.py`: runtime defaults and environment-backed settings
- `src/knowledge_base.py`: Chroma-backed knowledge-base retrieval
- `src/prompt.py`: prompt loading and prompt-context rendering
- `src/identity.py`: user identity resolution from room metadata and SIP participants
- `src/language.py`: language configuration and STT mode selection
- `src/tracing.py`: Langfuse OpenTelemetry integration
- `src/recording_config.py`: recording configuration from environment
- `src/recording_db.py`: PostgreSQL connection pool for recording metadata
- `src/recording_runtime.py`: recording lifecycle management
- `src/recording_store.py`: S3 upload operations
- `src/recording_transcript.py`: transcript formatting and upload
- `src/watchdog.py`: idle room detection and cleanup
- `PROMPT_DIAGNOSTIC.md`: system prompt template loaded at runtime
- `diagnostic-questions.json`: question bank seed data for ChromaDB
- `tests/`: unit tests

## Setup

```bash
cp .env.example .env.local
uv sync --group dev
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

## Knowledge Base Retrieval

The agent uses a ChromaDB knowledge base to retrieve assessment questions at runtime instead of loading the full question bank into prompt context. The `retrieve_knowledge` tool fetches relevant records during the conversation based on the current assessment stage.

## Prompt Context

The agent reads room metadata and injects values into `PROMPT_DIAGNOSTIC.md`.

Example room metadata:

```json
{
  "interaction_mode": "ptt",
  "prompt_context": {
    "agentName": "Maya",
    "userName": "Ravi"
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
- extra `prompt_context` keys can be referenced directly in `PROMPT_DIAGNOSTIC.md` as `{keyName}`
- `config.voice` overrides the default TTS speaker for the session
- `config.speakingSpeed` overrides the default TTS pace for the session

## Recording And Webhooks

Recording is optional.

- Set `ENABLE_RECORDING=false` to disable recording entirely
- When enabled, the agent stores audio and transcript in S3
- After egress completes and transcript upload succeeds, the agent sends a JSON `POST` request to `WEBHOOK_URL`

## Environment Variables

See `.env.example` for the full list.

### LiveKit

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVEKIT_URL` | | LiveKit server URL |
| `LIVEKIT_API_KEY` | | LiveKit API key |
| `LIVEKIT_API_SECRET` | | LiveKit API secret |
| `AGENT_NAME` | `diagnostic-agent` | LiveKit worker registration name |

### LLM

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | | OpenRouter API key |
| `OPENROUTER_MODEL` | `openai/gpt-5.1` | LLM model identifier |

### STT

| Variable | Default | Description |
|----------|---------|-------------|
| `SARVAM_API_KEY` | | Sarvam API key |
| `DEEPGRAM_API_KEY` | | Deepgram API key (alternative STT) |
| `DEEPGRAM_STT_LANGUAGE` | `en-IN` | Deepgram STT language |
| `DEEPGRAM_STT_MODEL` | `nova-3` | Deepgram STT model |

### TTS

| Variable | Default | Description |
|----------|---------|-------------|
| `SARVAM_TTS_LANGUAGE` | `en-IN` | Sarvam TTS target language |
| `SARVAM_TTS_MODEL` | `bulbul:v3` | Sarvam TTS model |
| `SARVAM_TTS_SPEAKER` | `ishita` | Sarvam TTS speaker voice |
| `SARVAM_TTS_DICT_ID` | `p_fcfdd23b` | Sarvam TTS dictionary ID |

### Knowledge Base (ChromaDB)

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_KNOWLEDGE_BASE` | `true` | Enable/disable Chroma knowledge base |
| `CHROMA_API_KEY` | | Chroma Cloud API key |
| `CHROMA_TENANT` | | Chroma Cloud tenant ID |
| `CHROMA_DATABASE` | | Chroma Cloud database name |
| `CHROMA_COLLECTION` | | Chroma collection name |
| `KNOWLEDGE_BASE_DEFAULT_LIMIT` | `10` | Default number of records to retrieve |

### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_PUBLIC_KEY` | | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | | Langfuse secret key |
| `LANGFUSE_HOST` | | Langfuse host URL |

### Recording And Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_RECORDING` | `true` | Enable/disable S3 egress recording |
| `DATABASE_URL` | | PostgreSQL connection string for recording metadata |
| `AWS_S3_BUCKET` | | S3 bucket name for recordings |
| `AWS_DEFAULT_REGION` | `us-east-1` | AWS region |
| `AWS_S3_ENDPOINT` | | Custom S3 endpoint (for S3-compatible storage) |
| `AWS_ACCESS_KEY_ID` | | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | | AWS secret key |
| `AWS_S3_FORCE_PATH_STYLE` | `false` | Force S3 path-style addressing |
| `S3_BASE_PREFIX` | `agents` | S3 key prefix for recordings |
| `WEBHOOK_URL` | | Webhook URL for post-recording notification |
| `EGRESS_POLL_TIMEOUT_SECONDS` | `45` | Timeout for egress polling |

## Deploy

```bash
lk agent create
```
