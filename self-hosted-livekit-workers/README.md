# Self-Hosted LiveKit Workers

This folder runs only the LiveKit agent workers for this repo:

- `CS-diagnostic-agent`
- `pre-screen-agent`
- `interview-agent`
- `job-agent`

It does not provision the LiveKit SFU, Redis, ingress, or TURN. Runtime credentials are loaded from the existing agent `.env` files in this repo.

## What Gets Built

Each worker service builds its own image from the agent directory:

- `../CS-diagnostic-agent`
- `../pre-screen-agent`
- `../interview-agent`
- `../job-agent`

The images are runtime-independent. Once built and pushed to a registry, the same images can be pulled onto a VM and scaled there without changing the application code.

## Setup

```bash
cd self-hosted-livekit-workers
cp .env.example .env
chmod +x manage.sh
```

Runtime env is read directly from:

- `../CS-diagnostic-agent/.env`
- `../pre-screen-agent/.env`
- `../interview-agent/.env`
- `../job-agent/.env`

The optional local `.env` in this folder is only for compose overrides like image tags and replica defaults.

## Agent Name Precedence

Agent registration name resolves in this order:

1. `self-hosted-livekit-workers/.env` compose override
2. per-agent `.env`
3. agent default in `src/constants.py`

For `CS-diagnostic-agent`, the intended default worker name is `diagnostic-agent-dev`.
If a VM already has an older `self-hosted-livekit-workers/.env`, it can still override that value. When deploying onto a VM, make sure the VM copy of `self-hosted-livekit-workers/.env` is either updated or removed if you want code defaults to win.

## Knowledge Base (ChromaDB)

All four agents use a ChromaDB knowledge base. The Chroma credentials are read from each agent's own `.env` file. The compose-level `.env` can optionally set shared `CHROMA_API_KEY`, `CHROMA_TENANT`, and `CHROMA_DATABASE` values, but per-agent `.env` files take precedence for their own agent.

## Start Workers

The default replica counts are:

- `cs-diagnostic-agent=2`
- `pre-screen-agent=2`
- `interview-agent=1`
- `job-agent=1`

```bash
./manage.sh up
```

## Scale Later On A VM

Change the counts in `self-hosted-livekit-workers/.env` and run:

```bash
./manage.sh up
```

Or scale immediately without editing the file:

```bash
./manage.sh scale 4 3 1 1
```

That sets:

- `CS-diagnostic-agent` to 4 workers
- `pre-screen-agent` to 3 workers
- `interview-agent` to 1 worker
- `job-agent` to 1 worker

## Use Prebuilt Images On The VM

If you do not want to build on the VM, push the images to your registry and update these compose override values:

```bash
CS_DIAGNOSTIC_AGENT_IMAGE=your-registry/cs-diagnostic-agent:tag
PRE_SCREEN_AGENT_IMAGE=your-registry/pre-screen-agent:tag
INTERVIEW_AGENT_IMAGE=your-registry/interview-agent:tag
JOB_AGENT_IMAGE=your-registry/job-agent:tag
```

Then pull and start:

```bash
./manage.sh pull
./manage.sh up
```

## Environment Variables

See `.env.example` for the full list. These are compose-level overrides.

### LiveKit

| Variable | Default | Description |
|----------|---------|-------------|
| `LIVEKIT_URL` | `wss://livekit.yourdomain.com` | LiveKit server URL |
| `LIVEKIT_API_KEY` | | LiveKit API key |
| `LIVEKIT_API_SECRET` | | LiveKit API secret |

### Model Providers

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | | OpenRouter API key |
| `OPENAI_API_KEY` | | OpenAI API key (fallback) |
| `SARVAM_API_KEY` | | Sarvam API key |
| `DEEPGRAM_API_KEY` | | Deepgram API key |

### Observability

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_PUBLIC_KEY` | | Langfuse public key |
| `LANGFUSE_SECRET_KEY` | | Langfuse secret key |
| `LANGFUSE_HOST` | `https://us.cloud.langfuse.com` | Langfuse host URL |

### Recording And Storage

| Variable | Default | Description |
|----------|---------|-------------|
| `ENABLE_RECORDING` | `false` | Enable/disable recording |
| `AWS_ACCESS_KEY_ID` | | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | | AWS secret key |
| `AWS_DEFAULT_REGION` | | AWS region |
| `S3_BUCKET_NAME` | | S3 bucket name |
| `S3_RECORDING_PREFIX` | | S3 prefix for recordings |
| `S3_TRANSCRIPT_PREFIX` | | S3 prefix for transcripts |
| `WEBHOOK_URL` | | Webhook URL for post-recording notification |

### Knowledge Base (ChromaDB)

| Variable | Default | Description |
|----------|---------|-------------|
| `CHROMA_API_KEY` | | Chroma Cloud API key |
| `CHROMA_TENANT` | | Chroma Cloud tenant ID |
| `CHROMA_DATABASE` | | Chroma Cloud database name |

### Agent Overrides

| Variable | Default | Description |
|----------|---------|-------------|
| `CS_DIAGNOSTIC_AGENT_NAME` | `diagnostic-agent` | CS diagnostic agent worker name |
| `CS_DIAGNOSTIC_AGENT_IMAGE` | `intervoo/cs-diagnostic-agent:latest` | CS diagnostic agent Docker image |
| `CS_DIAGNOSTIC_AGENT_REPLICAS` | `6` | CS diagnostic agent replica count |
| `PRE_SCREEN_AGENT_NAME` | `pre-screen-agent` | Pre-screen agent worker name |
| `PRE_SCREEN_AGENT_IMAGE` | `intervoo/pre-screen-agent:latest` | Pre-screen agent Docker image |
| `PRE_SCREEN_AGENT_REPLICAS` | `6` | Pre-screen agent replica count |
| `INTERVIEW_AGENT_NAME` | `interview-coaching-agent` | Interview agent worker name |
| `INTERVIEW_AGENT_IMAGE` | `intervoo/interview-agent:latest` | Interview agent Docker image |
| `INTERVIEW_AGENT_REPLICAS` | `1` | Interview agent replica count |
| `JOB_AGENT_NAME` | `job-finder-agent` | Job agent worker name |
| `JOB_AGENT_IMAGE` | `intervoo/job-agent:latest` | Job agent Docker image |
| `JOB_AGENT_REPLICAS` | `1` | Job agent replica count |

## Useful Commands

```bash
./manage.sh ps
./manage.sh logs
./manage.sh logs cs-diagnostic-agent
./manage.sh logs pre-screen-agent
./manage.sh logs interview-agent
./manage.sh logs job-agent
./manage.sh down
```
