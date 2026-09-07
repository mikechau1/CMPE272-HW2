#!/usr/bin/env bash
#
# Every route of the gateway, driven with HTTPie, with the expected status
# asserted for each. This is the runnable API-example artifact: it doubles as
# a smoke test, and each command is copy-pasteable straight out of the output.
#
#   ./scripts/httpie_examples.sh          # against http://localhost:$PORT
#   SERVICE_URL=https://x.ngrok-free.app ./scripts/httpie_examples.sh
#
# Needs a running service. `make install` puts HTTPie in .venv/bin.
#
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

# Prefer the project virtualenv so no system-wide HTTPie install is required.
if [[ -x .venv/bin/http ]]; then
  HTTP=".venv/bin/http"
elif command -v http >/dev/null 2>&1; then
  HTTP="http"
else
  echo "HTTPie not found. Run 'make install', or: pip install httpie" >&2
  exit 1
fi

[[ -f .env ]] && { set -a; source .env; set +a; }
: "${PORT:=8000}"
: "${SERVICE_URL:=http://localhost:${PORT}}"

PASS=0
FAIL=0
STATUS=""
BODY=""
HEADERS=""

bold()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
dim()   { printf '\033[2m  $ %s\033[0m\n' "$*"; }
ok()    { printf '  \033[32mPASS\033[0m %s\n' "$*"; PASS=$((PASS + 1)); }
bad()   { printf '  \033[31mFAIL\033[0m %s\n' "$*"; FAIL=$((FAIL + 1)); }

# Run HTTPie and split the response into STATUS / HEADERS / BODY.
# --pretty=none keeps the body raw so it can be parsed; --print=hb shows both.
req() {
  local out
  out="$("$HTTP" --ignore-stdin --print=hb --pretty=none --timeout=30 "$@" 2>&1)" || true
  STATUS="$(printf '%s\n' "$out" | head -1 | awk '{print $2}')"
  HEADERS="$(printf '%s\n' "$out" | sed -n '1,/^[[:space:]]*$/p')"
  BODY="$(printf '%s\n' "$out" | sed -n '/^[[:space:]]*$/,$p' | sed '1d')"
}

# Same, but the request body is the exact bytes of $1 (used for webhooks, where
# the signature must cover precisely what is transmitted).
#
# The body is an argument rather than piped in: piping into a shell function
# runs it in a subshell, so STATUS/BODY would be set there and lost here. The
# pipe lives inside the command substitution instead.
req_raw() {
  local body="$1"
  shift
  local out
  out="$(printf '%s' "$body" | "$HTTP" --print=hb --pretty=none --timeout=30 "$@" 2>&1)" || true
  STATUS="$(printf '%s\n' "$out" | head -1 | awk '{print $2}')"
  BODY="$(printf '%s\n' "$out" | sed -n '/^[[:space:]]*$/,$p' | sed '1d')"
}

expect() {
  local want="$1" label="$2"
  if [[ "$STATUS" == "$want" ]]; then
    ok "$label -> $STATUS"
  else
    bad "$label -> expected $want, got ${STATUS:-<no response>}"
    printf '       %s\n' "${BODY:0:400}"
  fi
}

json() { printf '%s' "$BODY" | python3 -c "import json,sys; print(json.load(sys.stdin)$1)" 2>/dev/null || echo ""; }
header() { printf '%s\n' "$HEADERS" | grep -i "^$1:" | sed "s/^[^:]*: *//" | tr -d '\r' || true; }

# ---------------------------------------------------------------------------
bold "Ops"
# ---------------------------------------------------------------------------

dim "http GET ${SERVICE_URL}/healthz"
req GET "${SERVICE_URL}/healthz"
expect 200 "healthz"

dim "http GET ${SERVICE_URL}/readyz deep==true"
req GET "${SERVICE_URL}/readyz" deep==true
expect 200 "readyz (deep: verifies the token can see the repo)"

dim "http GET ${SERVICE_URL}/openapi.yaml"
req GET "${SERVICE_URL}/openapi.yaml"
expect 200 "openapi.yaml"

# ---------------------------------------------------------------------------
bold "Issues"
# ---------------------------------------------------------------------------

dim "http POST ${SERVICE_URL}/issues title='...' body='...' labels:='[\"smoke\"]'"
req POST "${SERVICE_URL}/issues" \
  title="HTTPie example issue" \
  body="Created by scripts/httpie_examples.sh" \
  labels:='["smoke"]'
expect 201 "create issue"

NUMBER="$(json '["number"]')"
LOCATION="$(header Location)"
if [[ "$LOCATION" == "/issues/${NUMBER}" ]]; then
  ok "Location header points at /issues/${NUMBER}"
else
  bad "Location header was '${LOCATION}', expected /issues/${NUMBER}"
fi

dim "http POST ${SERVICE_URL}/issues body='no title'          # 400"
req POST "${SERVICE_URL}/issues" body="no title supplied"
expect 400 "create without a title is rejected"

dim "http GET ${SERVICE_URL}/issues state==open per_page==5"
req GET "${SERVICE_URL}/issues" state==open per_page==5
expect 200 "list issues"

ETAG="$(header ETag)"
printf '       ETag: %s\n' "${ETAG:-<none>}"
printf '       Link: %s\n' "$(header Link | head -c 160)"
printf '       X-Page: %s  X-Per-Page: %s  X-Cache: %s  X-RateLimit-Remaining: %s\n' \
  "$(header X-Page)" "$(header X-Per-Page)" "$(header X-Cache)" "$(header X-RateLimit-Remaining)"

if [[ -n "$ETAG" ]]; then
  dim "http GET ${SERVICE_URL}/issues state==open per_page==5 If-None-Match:'${ETAG}'   # 304"
  req GET "${SERVICE_URL}/issues" state==open per_page==5 "If-None-Match:${ETAG}"
  expect 304 "conditional GET costs no rate-limit budget"
fi

dim "http GET ${SERVICE_URL}/issues state==sideways                # 400"
req GET "${SERVICE_URL}/issues" state==sideways
expect 400 "invalid state filter is rejected"

dim "http GET ${SERVICE_URL}/issues/${NUMBER}"
req GET "${SERVICE_URL}/issues/${NUMBER}"
expect 200 "get issue"

dim "http GET ${SERVICE_URL}/issues/99999999                       # 404"
req GET "${SERVICE_URL}/issues/99999999"
expect 404 "unknown issue"

dim "http PATCH ${SERVICE_URL}/issues/${NUMBER} title='...'"
req PATCH "${SERVICE_URL}/issues/${NUMBER}" title="HTTPie example issue (renamed)"
expect 200 "rename issue"

dim "http PATCH ${SERVICE_URL}/issues/${NUMBER} state=closed state_reason=completed"
req PATCH "${SERVICE_URL}/issues/${NUMBER}" state=closed state_reason=completed
expect 200 "close issue -- this API's DELETE"
[[ "$(json '["state"]')" == "closed" ]] && ok "issue is closed" || bad "issue did not close"

dim "http PATCH ${SERVICE_URL}/issues/${NUMBER} state=open"
req PATCH "${SERVICE_URL}/issues/${NUMBER}" state=open
expect 200 "reopen issue"

dim "http PATCH ${SERVICE_URL}/issues/${NUMBER} state=deleted      # 400"
req PATCH "${SERVICE_URL}/issues/${NUMBER}" state=deleted
expect 400 "invalid state is rejected"

# ---------------------------------------------------------------------------
bold "Comments"
# ---------------------------------------------------------------------------

dim "http POST ${SERVICE_URL}/issues/${NUMBER}/comments body='...'"
req POST "${SERVICE_URL}/issues/${NUMBER}/comments" body="Comment from scripts/httpie_examples.sh"
expect 201 "create comment"

dim "http GET ${SERVICE_URL}/issues/${NUMBER}/comments per_page==30"
req GET "${SERVICE_URL}/issues/${NUMBER}/comments" per_page==30
expect 200 "list comments"

# ---------------------------------------------------------------------------
bold "Webhooks"
# ---------------------------------------------------------------------------

if [[ -z "${WEBHOOK_SECRET:-}" ]]; then
  echo "  (skipped: WEBHOOK_SECRET is not set in .env)"
else
  PAYLOAD='{"action":"opened","issue":{"number":12,"title":"HTTPie example"},"repository":{"full_name":"'"${GITHUB_OWNER:-owner}/${GITHUB_REPO:-repo}"'"},"sender":{"login":"'"${GITHUB_OWNER:-owner}"'"}}'
  SIG="sha256=$(printf '%s' "$PAYLOAD" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"
  DELIVERY="$(uuidgen 2>/dev/null || python3 -c 'import uuid; print(uuid.uuid4())')"

  dim "printf '%s' \"\$PAYLOAD\" | http POST ${SERVICE_URL}/webhook X-GitHub-Event:issues X-Hub-Signature-256:\"\$SIG\""
  req_raw "$PAYLOAD" POST "${SERVICE_URL}/webhook" \
    Content-Type:application/json \
    X-GitHub-Event:issues \
    "X-GitHub-Delivery:${DELIVERY}" \
    "X-Hub-Signature-256:${SIG}"
  expect 204 "signed delivery is accepted"

  dim "# same delivery id again -- idempotent, no second stored event"
  req_raw "$PAYLOAD" POST "${SERVICE_URL}/webhook" \
    Content-Type:application/json \
    X-GitHub-Event:issues \
    "X-GitHub-Delivery:${DELIVERY}" \
    "X-Hub-Signature-256:${SIG}"
  expect 204 "redelivery is acked, not rejected"

  dim "# sign one body, send another -- the attack the HMAC exists to stop"
  req_raw "${PAYLOAD/opened/closed}" POST "${SERVICE_URL}/webhook" \
    Content-Type:application/json \
    X-GitHub-Event:issues \
    X-GitHub-Delivery:tampered \
    "X-Hub-Signature-256:${SIG}"
  expect 401 "tampered body is rejected"

  PUSH='{"ref":"refs/heads/main"}'
  PUSH_SIG="sha256=$(printf '%s' "$PUSH" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"
  dim "# a correctly signed but unsupported event"
  req_raw "$PUSH" POST "${SERVICE_URL}/webhook" \
    Content-Type:application/json \
    X-GitHub-Event:push \
    "X-Hub-Signature-256:${PUSH_SIG}"
  expect 400 "unsupported event is rejected"

  PING='{"zen":"Non-blocking is better than blocking.","hook_id":1}'
  PING_SIG="sha256=$(printf '%s' "$PING" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"
  dim "# ping, which GitHub sends the moment you save a webhook"
  req_raw "$PING" POST "${SERVICE_URL}/webhook" \
    Content-Type:application/json \
    X-GitHub-Event:ping \
    "X-GitHub-Delivery:ping-$(date +%s)" \
    "X-Hub-Signature-256:${PING_SIG}"
  expect 204 "ping is accepted"

  dim "http POST ${SERVICE_URL}/webhook X-GitHub-Event:issues       # unsigned -> 401"
  req POST "${SERVICE_URL}/webhook" X-GitHub-Event:issues action=opened
  expect 401 "unsigned delivery is rejected"
fi

# ---------------------------------------------------------------------------
bold "Events"
# ---------------------------------------------------------------------------

dim "http GET ${SERVICE_URL}/events limit==10"
req GET "${SERVICE_URL}/events" limit==10
expect 200 "list recent deliveries"

if [[ -n "${WEBHOOK_SECRET:-}" ]]; then
  COUNT="$(printf '%s' "$BODY" | python3 -c \
    "import json,sys; print(sum(1 for e in json.load(sys.stdin) if e['delivery_id']=='${DELIVERY}'))" 2>/dev/null || echo "?")"
  if [[ "$COUNT" == "1" ]]; then
    ok "the twice-delivered event is stored exactly once"
  else
    bad "expected 1 stored row for delivery ${DELIVERY}, found ${COUNT}"
  fi
fi

printf '%s' "$BODY" | python3 -m json.tool 2>/dev/null | head -20 | sed 's/^/       /' || true

# ---------------------------------------------------------------------------
bold "Result"
printf '  %d passed, %d failed\n' "$PASS" "$FAIL"
[[ -n "${NUMBER:-}" ]] && printf '  Issue #%s is open in %s/%s\n' \
  "$NUMBER" "${GITHUB_OWNER:-?}" "${GITHUB_REPO:-?}"
exit $(( FAIL > 0 ? 1 : 0 ))
