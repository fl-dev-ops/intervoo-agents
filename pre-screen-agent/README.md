# Pre-Screen Agent

Standalone LiveKit voice agent for career pre-screening. Conducts conversational assessments with college students to understand their job plans and placement awareness. Deployed independently with the worker name `pre-screen-agent`.

## Stack

- Runtime: Python 3.10+
- Agent framework: LiveKit Agents
- STT: Sarvam STT (saaras:v3)
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
- `PROMPT.md`: system prompt v1 (reference)
- `PROMPT-v2.md`: system prompt v2 (active, voice-optimized)
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

The agent uses a ChromaDB knowledge base to retrieve pre-screening questions at runtime. The `retrieve_knowledge` tool fetches relevant records during the conversation based on the student's path and conversation stage.

## Room Metadata

Use room metadata to control turn behavior and inject prompt variables per session.

```json
{
  "interaction_mode": "ptt",
  "prompt_context": {
    "agentName": "Maya",
    "userName": "Ravi",
    "prompt": "Keep the tone slightly more formal."
  }
}
```

Rules:

- `interaction_mode` supports only `auto` and `ptt`
- `prompt_context` is a flat object of prompt placeholders
- built-in defaults always exist for `{agentName}` and `{userName}`
- the default `{agentName}` value is configured in `src/constants.py`
- any extra `prompt_context` keys can be referenced directly in `PROMPT-v2.md` as `{keyName}`

## Recording Toggle

Set `ENABLE_RECORDING=false` to disable the S3 egress recording flow even when the S3 environment variables are present.

When recording is enabled, the agent stores audio and transcript in S3. After the egress completes successfully and the transcript upload finishes, it sends a JSON `POST` request to `WEBHOOK_URL`.

## Environment Variables

See `.env.example` for the full list.

### LiveKit

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVEKIT_URL` | | LiveKit server URL |
| `LIVEKIT_API_KEY` | | LiveKit API key |
| `LIVEKIT_API_SECRET` | | LiveKit API secret |
| `AGENT_NAME` | `pre-screen-agent` | LiveKit worker registration name |

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
