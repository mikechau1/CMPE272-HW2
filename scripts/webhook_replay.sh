#!/usr/bin/env bash
#
# Replay a captured webhook payload at a running gateway, signed the way GitHub
# signs it. Demonstrates signature verification and idempotency without needing
# a tunnel.
#
#   ./scripts/webhook_replay.sh                                  # issues/opened
#   ./scripts/webhook_replay.sh tests/fixtures/webhook_ping.json ping
#   ./scripts/webhook_replay.sh <payload.json> <event> --tamper  # expect 401
#
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PAYLOAD="${1:-tests/fixtures/webhook_issues_opened.json}"
EVENT="${2:-issues}"
TAMPER="${3:-}"

if [[ -x .venv/bin/http ]]; then
  HTTP=".venv/bin/http"
elif command -v http >/dev/null 2>&1; then
  HTTP="http"
else
  echo "HTTPie not found. Run 'make install', or: pip install httpie" >&2
  exit 1
fi

[[ -f .env ]] || { echo "No .env found. Run ./scripts/run.sh first." >&2; exit 1; }
[[ -f "$PAYLOAD" ]] || { echo "No such payload file: $PAYLOAD" >&2; exit 1; }

set -a
# shellcheck disable=SC1091
source .env
set +a
: "${PORT:=8000}"
: "${SERVICE_URL:=http://localhost:${PORT}}"
[[ -n "${WEBHOOK_SECRET:-}" ]] || { echo "WEBHOOK_SECRET is not set in .env" >&2; exit 1; }

BODY="$(cat "$PAYLOAD")"
# Sign the exact bytes that will be sent -- GitHub signs the raw body, so the
# digest and the transmitted payload must not differ by even a newline.
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"
DELIVERY="$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')"

if [[ "$TAMPER" == "--tamper" ]]; then
  # Same signature, different bytes: the attack the HMAC exists to stop.
  BODY="${BODY/\"opened\"/\"closed\"}"
  echo "Tampering with the body after signing -- expecting 401."
fi

deliver() {
  printf '%s' "$BODY" | "$HTTP" --print=hb --timeout=30 POST "${SERVICE_URL}/webhook" \
    Content-Type:application/json \
    "X-GitHub-Event:${EVENT}" \
    "X-GitHub-Delivery:${DELIVERY}" \
    "X-Hub-Signature-256:${SIG}" \
    User-Agent:GitHub-Hookshot/replay
}

echo "POST ${SERVICE_URL}/webhook   event=${EVENT}   delivery=${DELIVERY}"
deliver | sed 's/^/  /'

echo
if [[ "$TAMPER" == "--tamper" ]]; then
  echo "Expected 401 above, and the delivery must not be stored."
else
  echo "Replaying the same delivery id (expecting another 204, and no new event):"
  deliver | head -1 | sed 's/^/  /'
fi

echo
echo "GET ${SERVICE_URL}/events?limit=5"
# --pretty=format keeps the JSON indented even though stdout is a pipe.
"$HTTP" --ignore-stdin --pretty=format GET "${SERVICE_URL}/events" limit==5 | sed 's/^/  /'
