#!/usr/bin/env bash
#
# One-click local run.  Creates a virtualenv, installs dependencies, checks the
# environment, and starts the gateway.
#
#   ./scripts/run.sh              # start the service
#   ./scripts/run.sh --docker     # build and run the container instead
#
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*" >&2; }
die()  { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }

# --- .env ------------------------------------------------------------------
if [[ ! -f .env ]]; then
  warn "No .env found; creating one from .env.example."
  cp .env.example .env
  if command -v openssl >/dev/null 2>&1; then
    secret="$(openssl rand -hex 32)"
    # macOS and GNU sed disagree about -i, so write through a temp file.
    sed "s|^WEBHOOK_SECRET=.*|WEBHOOK_SECRET=${secret}|" .env > .env.tmp && mv .env.tmp .env
    bold "Generated a random WEBHOOK_SECRET in .env"
  fi
  warn "Edit .env and set GITHUB_TOKEN, GITHUB_OWNER and GITHUB_REPO, then re-run."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

missing=()
for var in GITHUB_TOKEN GITHUB_OWNER GITHUB_REPO WEBHOOK_SECRET; do
  [[ -n "${!var:-}" ]] || missing+=("$var")
done
if (( ${#missing[@]} )); then
  die "Missing required environment variables in .env: ${missing[*]}"
fi
: "${PORT:=8000}"

# --- docker path -----------------------------------------------------------
if [[ "${1:-}" == "--docker" ]]; then
  command -v docker >/dev/null 2>&1 || die "docker is not installed or not on PATH."
  bold "Building issues-gateway:local"
  docker build -t issues-gateway:local .
  bold "Starting container on http://localhost:${PORT}"
  exec docker run --rm -it --name issues-gateway \
    --env-file .env \
    -e PORT=8000 -e EVENT_STORE_PATH=/data/events.db \
    -p "${PORT}:8000" \
    -v issues-gateway-data:/data \
    issues-gateway:local
fi

# --- local python path -----------------------------------------------------
command -v python3 >/dev/null 2>&1 || die "python3 is not installed or not on PATH."

if [[ ! -d .venv ]]; then
  bold "Creating virtualenv (.venv)"
  python3 -m venv .venv
fi

if [[ requirements.txt -nt .venv/.deps-installed ]] || [[ ! -f .venv/.deps-installed ]]; then
  bold "Installing dependencies"
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
  touch .venv/.deps-installed
fi

bold "issues-gateway -> http://localhost:${PORT}"
echo "  docs      http://localhost:${PORT}/docs"
echo "  contract  http://localhost:${PORT}/openapi.yaml"
echo "  health    http://localhost:${PORT}/healthz"
echo "  events    http://localhost:${PORT}/events"
echo "  repo      ${GITHUB_OWNER}/${GITHUB_REPO}"
echo

exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
