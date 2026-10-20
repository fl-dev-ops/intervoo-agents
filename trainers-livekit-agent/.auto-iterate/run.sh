#!/usr/bin/env bash
set -euo pipefail

POLICY_PATH="${1:?policy path is required}"
RESULT_DIR="${2:?result directory is required}"
MODE="${3:-policy}"
AUTO_ITERATE="$(cd "$(dirname "$0")" && pwd)"

mkdir -p "$RESULT_DIR"

# Credentials come from .auto-iterate/.env (gitignored) or the surrounding environment.
if [[ -f "$AUTO_ITERATE/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$AUTO_ITERATE/.env"
  set +a
fi

if [[ -z "${OPENROUTER_API_KEY:-}${OPENAI_API_KEY:-}" ]]; then
  echo "Set OPENROUTER_API_KEY in $AUTO_ITERATE/.env or the environment" >&2
  exit 1
fi

export MODEL="${MODEL:-openai/gpt-4o}"
export JUDGE_MODEL="${JUDGE_MODEL:-$MODEL}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-https://openrouter.ai/api/v1}"

uv run --with openai --with pydantic python "$AUTO_ITERATE/run_experiment.py" \
  --policy "$POLICY_PATH" \
  --output-dir "$RESULT_DIR" \
  --mode "$MODE"
