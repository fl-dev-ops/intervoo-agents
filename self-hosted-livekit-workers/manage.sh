#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="${ROOT_DIR}/docker-compose.yml"
ENV_FILE="${ENV_FILE:-${ROOT_DIR}/.env}"

CS_REPLICAS="${CS_DIAGNOSTIC_AGENT_REPLICAS:-2}"
PRE_REPLICAS="${PRE_SCREEN_AGENT_REPLICAS:-2}"
INTERVIEW_REPLICAS="${INTERVIEW_AGENT_REPLICAS:-1}"
JOB_REPLICAS="${JOB_AGENT_REPLICAS:-1}"

compose() {
  local args=()
  if [[ -f "${ENV_FILE}" ]]; then
    args+=(--env-file "${ENV_FILE}")
  fi
  docker compose "${args[@]}" -f "${COMPOSE_FILE}" "$@"
}

usage() {
  cat <<'EOF'
Usage: ./manage.sh <command> [args]

Commands:
  up                  Build images and start all worker services with the configured replica counts
  build               Build all worker images
  pull                Pull the configured worker images
  ps                  Show worker containers
  logs [service]      Tail logs for all workers or one service
  down                Stop and remove worker containers
  restart             Restart running worker containers
  config              Render the resolved compose config
  scale [cs] [pre] [interview] [job]
                       Change replica counts without changing the image

Environment:
  ENV_FILE                            Optional path to compose override values
  CS_DIAGNOSTIC_AGENT_REPLICAS        Default replicas for cs-diagnostic-agent
  PRE_SCREEN_AGENT_REPLICAS           Default replicas for pre-screen-agent
  INTERVIEW_AGENT_REPLICAS            Default replicas for interview-agent
  JOB_AGENT_REPLICAS                  Default replicas for job-agent
EOF
}

COMMAND="${1:-up}"
shift || true

case "${COMMAND}" in
  up)
    compose up -d --build \
      --scale "cs-diagnostic-agent=${CS_REPLICAS}" \
      --scale "pre-screen-agent=${PRE_REPLICAS}" \
      --scale "interview-agent=${INTERVIEW_REPLICAS}" \
      --scale "job-agent=${JOB_REPLICAS}"
    ;;
  build)
    compose build
    ;;
  pull)
    compose pull
    ;;
  ps)
    compose ps
    ;;
  logs)
    if [[ $# -gt 0 ]]; then
      compose logs -f --tail=100 "$1"
    else
      compose logs -f --tail=100
    fi
    ;;
  down)
    compose down
    ;;
  restart)
    compose restart
    ;;
  config)
    compose config
    ;;
  scale)
    cs_target="${1:-${CS_REPLICAS}}"
    pre_target="${2:-${PRE_REPLICAS}}"
    interview_target="${3:-${INTERVIEW_REPLICAS}}"
    job_target="${4:-${JOB_REPLICAS}}"
    compose up -d \
      --scale "cs-diagnostic-agent=${cs_target}" \
      --scale "pre-screen-agent=${pre_target}" \
      --scale "interview-agent=${interview_target}" \
      --scale "job-agent=${job_target}"
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: ${COMMAND}"
    usage
    exit 1
    ;;
esac
