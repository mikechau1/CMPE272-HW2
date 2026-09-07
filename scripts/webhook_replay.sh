#!/usr/bin/env bash
#
# Replay a signed webhook delivery at a running gateway, the way GitHub would.
# Useful for demoing signature verification and idempotency without a tunnel.
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
# Sign the exact bytes that will be sent -- GitHub signs the raw body.
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"
DELIVERY="$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')"

if [[ "$TAMPER" == "--tamper" ]]; then
  # Same signature, different bytes: this is the attack the HMAC exists to stop.
  BODY="${BODY/\"opened\"/\"closed\"}"
  echo "Tampering with the body after signing -- expecting 401."
fi

echo "POST ${SERVICE_URL}/webhook   event=${EVENT}   delivery=${DELIVERY}"
printf '%s' "$BODY" | curl -sS -i -X POST "${SERVICE_URL}/webhook" \
  -H 'Content-Type: application/json' \
  -H "X-GitHub-Event: ${EVENT}" \
  -H "X-GitHub-Delivery: ${DELIVERY}" \
  -H "X-Hub-Signature-256: ${SIG}" \
  -H 'User-Agent: GitHub-Hookshot/replay' \
  --data-binary @- | sed 's/^/  /'

echo
if [[ "$TAMPER" == "--tamper" ]]; then
  echo "Expected 401 above, and the delivery must not be stored."
else
  echo "Replaying the same delivery id (expecting another 204, and no new event):"
  printf '%s' "$BODY" | curl -sS -o /dev/null -w '  HTTP %{http_code}\n' -X POST "${SERVICE_URL}/webhook" \
    -H 'Content-Type: application/json' \
    -H "X-GitHub-Event: ${EVENT}" \
    -H "X-GitHub-Delivery: ${DELIVERY}" \
    -H "X-Hub-Signature-256: ${SIG}" \
    --data-binary @-
fi

echo
echo "GET ${SERVICE_URL}/events?limit=5"
curl -sS "${SERVICE_URL}/events?limit=5" | python3 -m json.tool | sed 's/^/  /'
