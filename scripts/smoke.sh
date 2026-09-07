#!/usr/bin/env bash
#
# Drive every route against a running gateway: create, read, list, update,
# close, reopen, comment, and a couple of deliberate 4xx cases.
#
#   ./scripts/smoke.sh
#
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

[[ -f .env ]] && { set -a; source .env; set +a; }
: "${PORT:=8000}"
: "${SERVICE_URL:=http://localhost:${PORT}}"

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
jsn()  { python3 -m json.tool 2>/dev/null || cat; }

step "GET /healthz"
curl -sS "${SERVICE_URL}/healthz" | jsn

step "POST /issues"
CREATE="$(curl -sS -D /tmp/gw-headers -X POST "${SERVICE_URL}/issues" \
  -H 'Content-Type: application/json' \
  -d '{"title":"smoke test issue","body":"created by scripts/smoke.sh","labels":["smoke"]}')"
echo "$CREATE" | jsn
grep -i '^location:' /tmp/gw-headers || true

NUMBER="$(printf '%s' "$CREATE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["number"])')"
echo "issue number: ${NUMBER}"

step "GET /issues/${NUMBER}"
curl -sS "${SERVICE_URL}/issues/${NUMBER}" | jsn

step "GET /issues?state=open&per_page=5  (note the Link and ETag headers)"
curl -sS -D /tmp/gw-list-headers -o /tmp/gw-list-body "${SERVICE_URL}/issues?state=open&per_page=5"
grep -iE '^(link|etag|x-page|x-per-page|x-cache|x-ratelimit-remaining):' /tmp/gw-list-headers || true

step "GET /issues with If-None-Match  (expect 304, and no rate-limit spend)"
ETAG="$(grep -i '^etag:' /tmp/gw-list-headers | sed 's/^[Ee][Tt][Aa][Gg]: *//' | tr -d '\r')"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  -H "If-None-Match: ${ETAG}" "${SERVICE_URL}/issues?state=open&per_page=5"

step "POST /issues/${NUMBER}/comments"
curl -sS -X POST "${SERVICE_URL}/issues/${NUMBER}/comments" \
  -H 'Content-Type: application/json' \
  -d '{"body":"comment from scripts/smoke.sh"}' | jsn

step "GET /issues/${NUMBER}/comments"
curl -sS "${SERVICE_URL}/issues/${NUMBER}/comments" | jsn

step "PATCH /issues/${NUMBER}  (rename)"
curl -sS -X PATCH "${SERVICE_URL}/issues/${NUMBER}" \
  -H 'Content-Type: application/json' \
  -d '{"title":"smoke test issue (renamed)"}' | jsn

step "PATCH /issues/${NUMBER}  (close -- this API's DELETE)"
curl -sS -X PATCH "${SERVICE_URL}/issues/${NUMBER}" \
  -H 'Content-Type: application/json' \
  -d '{"state":"closed","state_reason":"completed"}' | jsn

step "PATCH /issues/${NUMBER}  (reopen)"
curl -sS -X PATCH "${SERVICE_URL}/issues/${NUMBER}" \
  -H 'Content-Type: application/json' -d '{"state":"open"}' | jsn

step "Error cases"
echo "-- missing title (expect 400)"
curl -sS -o /dev/null -w '   HTTP %{http_code}\n' -X POST "${SERVICE_URL}/issues" \
  -H 'Content-Type: application/json' -d '{}'
echo "-- invalid state filter (expect 400)"
curl -sS -o /dev/null -w '   HTTP %{http_code}\n' "${SERVICE_URL}/issues?state=sideways"
echo "-- unknown issue (expect 404)"
curl -sS -o /dev/null -w '   HTTP %{http_code}\n' "${SERVICE_URL}/issues/99999999"
echo "-- unsigned webhook (expect 401)"
curl -sS -o /dev/null -w '   HTTP %{http_code}\n' -X POST "${SERVICE_URL}/webhook" \
  -H 'Content-Type: application/json' -H 'X-GitHub-Event: issues' -d '{"action":"opened"}'

step "GET /events"
curl -sS "${SERVICE_URL}/events?limit=5" | jsn

printf '\n\033[1mSmoke test finished. Issue #%s is open in your test repo.\033[0m\n' "${NUMBER}"
