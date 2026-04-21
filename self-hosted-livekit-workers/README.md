# Self-Hosted LiveKit Workers

This folder runs only the LiveKit agent workers for this repo:

- `CS-diagnostic-agent`
- `pre-screen-agent`

It does not provision the LiveKit SFU, Redis, ingress, or TURN. Runtime credentials are loaded from the existing agent `.env` files in this repo.

## What Gets Built

Each worker service builds its own image from the agent directory:

- `../CS-diagnostic-agent`
- `../pre-screen-agent`

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

The optional local `.env` in this folder is only for compose overrides like image tags and replica defaults.

## Agent Name Precedence

Agent registration name resolves in this order:

1. `self-hosted-livekit-workers/.env` compose override
2. per-agent `.env`
3. agent default in `src/constants.py`

For `CS-diagnostic-agent`, the intended default worker name is `diagnostic-agent-dev`.
If a VM already has an older `self-hosted-livekit-workers/.env`, it can still override that value. When deploying onto a VM, make sure the VM copy of `self-hosted-livekit-workers/.env` is either updated or removed if you want code defaults to win.

## Start 2 Workers Per Agent

The default replica counts are `2` for both services, even if you do not create a local compose `.env`.

```bash
./manage.sh up
```

This resolves to:

- `cs-diagnostic-agent=2`
- `pre-screen-agent=2`

## Scale Later On A VM

Change the counts in `self-hosted-livekit-workers/.env` and run:

```bash
./manage.sh up
```

Or scale immediately without editing the file:

```bash
./manage.sh scale 4 3
```

That sets:

- `CS-diagnostic-agent` to 4 workers
- `pre-screen-agent` to 3 workers

## Use Prebuilt Images On The VM

If you do not want to build on the VM, push the images to your registry and update these compose override values:

```bash
CS_DIAGNOSTIC_AGENT_IMAGE=your-registry/cs-diagnostic-agent:tag
PRE_SCREEN_AGENT_IMAGE=your-registry/pre-screen-agent:tag
```

Then pull and start:

```bash
./manage.sh pull
./manage.sh up
```

## Useful Commands

```bash
./manage.sh ps
./manage.sh logs
./manage.sh logs cs-diagnostic-agent
./manage.sh logs pre-screen-agent
./manage.sh down
```
