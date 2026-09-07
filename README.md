# GitHub Issues Gateway

CMPE 272 — Homework 2

A small, production-shaped HTTP service that fronts the **GitHub Issues REST API**
for one repository and ingests that repository's **webhooks**.

- Contract-first: [`openapi.yaml`](openapi.yaml) (OpenAPI 3.1) is the source of
  truth and is what the service actually serves at `/openapi.json` and `/docs`.
- HMAC-SHA256 webhook verification with constant-time comparison, and a
  delivery store that makes redelivery a no-op.
- Rate-limit aware, paginating, retry-aware GitHub client with conditional GET.
- 320 automated tests; 96% line coverage; 16 of them run against real GitHub.

**Stack:** Python 3.11+ / FastAPI / httpx / SQLite. **Testing:** pytest + respx.

---

## Table of contents

- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Creating the token and the repo](#creating-the-token-and-the-repo)
- [API reference with examples](#api-reference-with-examples)
- [Webhook setup](#webhook-setup)
- [Running the tests](#running-the-tests)
- [Docker](#docker)
- [How it works](#how-it-works)
- [Project layout](#project-layout)
- [Make targets](#make-targets)

---

## Quick start

```bash
git clone https://github.com/mikechau1/cmpe272-issues-gw.git
cd cmpe272-issues-gw

cp .env.example .env
$EDITOR .env          # set GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO, WEBHOOK_SECRET

./scripts/run.sh      # creates the venv, installs deps, starts the service
```

Then:

| What | Where |
| --- | --- |
| Interactive docs | <http://localhost:8000/docs> |
| The contract | <http://localhost:8000/openapi.yaml> |
| Health | <http://localhost:8000/healthz> |
| Recent deliveries | <http://localhost:8000/events> |

`scripts/run.sh` is idempotent: it creates `.env` from the template with a
freshly generated `WEBHOOK_SECRET` on first run, and reuses the virtualenv
afterwards. `./scripts/run.sh --docker` does the same thing in a container.

To exercise every route against your real repository in one go:

```bash
./scripts/smoke.sh
```

---

## Configuration

Everything is an environment variable; nothing is read from a config file
that could be committed. `.env` is git-ignored and is loaded automatically.

### Required

| Variable | Meaning |
| --- | --- |
| `GITHUB_TOKEN` | Fine-grained PAT (or GitHub App installation token) with **Issues: Read and write** |
| `GITHUB_OWNER` | Repository owner, e.g. `mikechau1` |
| `GITHUB_REPO` | Repository name, e.g. `cmpe272-issues-gw` |
| `WEBHOOK_SECRET` | Shared secret for webhook HMAC. `openssl rand -hex 32` |
| `PORT` | Port to listen on (default `8000`) |

### Optional

| Variable | Default | Meaning |
| --- | --- | --- |
| `GITHUB_API_URL` | `https://api.github.com` | Point at GitHub Enterprise if needed |
| `LOG_LEVEL` | `INFO` | Standard Python levels |
| `LOG_FORMAT` | `json` | `json` for production, `text` for readable local output |
| `EVENT_STORE_PATH` | `data/events.db` | SQLite path; `:memory:` for an ephemeral store |
| `EVENT_RETENTION` | `500` | How many deliveries to keep |
| `GITHUB_TIMEOUT_S` | `10.0` | Per-request upstream timeout |
| `GITHUB_MAX_RETRIES` | `3` | Retries for *safe* methods only (see [design note](DESIGN.md)) |
| `ENABLE_ETAG_CACHE` | `true` | Conditional GET / `If-None-Match` |
| `MAX_WEBHOOK_BODY_BYTES` | `5242880` | Reject oversized deliveries with 413 |

The service starts with an incomplete environment rather than crash-looping —
`/healthz` reports exactly which variables are missing, and the routes that
need them answer `401`/`503` with a message naming the variable.

---

## Creating the token and the repo

### 1. A dedicated test repository

```bash
gh repo create cmpe272-issues-gw --private \
  --description "CMPE 272 HW2 - HTTP gateway over the GitHub Issues REST API"
```

Any repository works as long as Issues are enabled. Use a throwaway one: the
integration tests create real issues in it.

### 2. A fine-grained PAT (least privilege)

1. Go to **Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token**
   (<https://github.com/settings/personal-access-tokens/new>).
2. **Resource owner:** your account. **Repository access:** *Only select
   repositories* → pick `cmpe272-issues-gw`.
3. **Repository permissions:**
   - **Issues: Read and write** ← the only one you need
   - *Metadata: Read-only* is granted automatically as a dependency
4. Leave every other permission at *No access*. Set a short expiry.
5. Copy the `github_pat_…` value into `GITHUB_TOKEN` in `.env`.

**Why these scopes.** `Issues: Read and write` covers create, read, update,
close/reopen, and comments — the whole surface of this API. It does **not**
grant access to code, Actions, secrets, or any other repository. A classic PAT
with `repo` would work but grants far more than this service needs, so it is
not recommended.

**Do not commit the token.** `.env` and `.env.*` are in `.gitignore`. For
long-term storage put it in your OS keychain and export it at shell startup:

```bash
# macOS
security add-generic-password -a "$USER" -s cmpe272-github-token -w 'github_pat_...'
export GITHUB_TOKEN="$(security find-generic-password -a "$USER" -s cmpe272-github-token -w)"
```

If you leak a token, revoke it immediately at
<https://github.com/settings/personal-access-tokens>.

---

## API reference with examples

Base URL: `http://localhost:${PORT}`. Every example below is copy-pasteable.

Clients of this API are unauthenticated — it is meant to run on localhost or
behind your own gateway. The service holds the GitHub credential; it never
accepts one from the caller.

### 1. `POST /issues` — create

```bash
curl -i -X POST http://localhost:8000/issues \
  -H 'Content-Type: application/json' \
  -d '{
        "title": "Rate limiter drops the Retry-After header",
        "body":  "Reproduced on main at 0f21ac9.",
        "labels": ["bug", "gateway"]
      }'
```

```http
HTTP/1.1 201 Created
Location: /issues/12
X-Request-ID: 8f14e45fceea167a5a36dedd4bea2543
X-RateLimit-Remaining: 4993

{"number":12,"id":2451900001,"title":"Rate limiter drops the Retry-After header",
 "body":"Reproduced on main at 0f21ac9.","state":"open","state_reason":null,
 "labels":[{"name":"bug","color":"d73a4a","description":"Something isn't working"}],
 "user":{"login":"mikechau1","id":583920,"type":"User", "...":"..."},
 "assignees":[],"comments":0,"locked":false,
 "html_url":"https://github.com/mikechau1/cmpe272-issues-gw/issues/12",
 "created_at":"2026-09-06T18:20:11Z","updated_at":"2026-09-06T18:20:11Z","closed_at":null}
```

HTTPie:

```bash
http POST :8000/issues title="Something is broken" body="Details here" labels:='["bug"]'
```

Invalid payload → `400` (not 422; the contract specifies 400):

```bash
curl -s -X POST http://localhost:8000/issues \
  -H 'Content-Type: application/json' -d '{}' | jq
```

```json
{
  "error": {
    "code": "validation_error",
    "message": "Request validation failed -- body.title: Field required",
    "status": 400,
    "request_id": "8f14e45fceea167a5a36dedd4bea2543",
    "details": [{ "field": "body.title", "message": "Field required", "type": "missing", "input": {} }]
  }
}
```

### 2. `GET /issues` — list

```bash
curl -i 'http://localhost:8000/issues?state=open&labels=bug&page=1&per_page=5'
```

```http
HTTP/1.1 200 OK
Link: </issues?state=open&labels=bug&page=2&per_page=5>; rel="next",
      </issues?state=open&labels=bug&page=9&per_page=5>; rel="last"
ETag: W/"gw1-a1b2c3d4e5f6"
X-Page: 1
X-Per-Page: 5
X-Cache: MISS
X-RateLimit-Remaining: 4992
```

Query parameters: `state` (`open`|`closed`|`all`, default `open`), `labels`
(comma-separated), `page` (≥1), `per_page` (1–100, default 30), `sort`
(`created`|`updated`|`comments`), `direction` (`asc`|`desc`).

The `Link` header is GitHub's pagination, **rewritten to point at this
service** and carrying your filters forward, so you can follow `rel="next"`
directly. Pull requests are filtered out — GitHub serves them from the same
endpoint, but this is an issues API.

**Conditional GET (extra credit).** Send the `ETag` back and get a `304` that
costs no GitHub rate-limit budget:

```bash
ETAG=$(curl -sD - -o /dev/null 'http://localhost:8000/issues?per_page=5' \
        | awk -F': ' 'tolower($1)=="etag"{print $2}' | tr -d '\r')

curl -s -o /dev/null -w '%{http_code}\n' \
  -H "If-None-Match: $ETAG" 'http://localhost:8000/issues?per_page=5'
# 304
```

### 3. `GET /issues/{number}` — read one

```bash
curl -s http://localhost:8000/issues/12 | jq '{number, title, state, comments}'
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/issues/99999999   # 404
```

### 4. `PATCH /issues/{number}` — update, close, reopen

Only the keys you send are forwarded, so a partial update cannot clobber
fields you did not mention.

```bash
# rename
curl -s -X PATCH http://localhost:8000/issues/12 \
  -H 'Content-Type: application/json' -d '{"title":"Rate limiter drops Retry-After (confirmed)"}'

# edit the body
curl -s -X PATCH http://localhost:8000/issues/12 \
  -H 'Content-Type: application/json' -d '{"body":"Updated repro steps."}'

# close -- this API's DELETE
curl -s -X PATCH http://localhost:8000/issues/12 \
  -H 'Content-Type: application/json' -d '{"state":"closed","state_reason":"completed"}'

# reopen
curl -s -X PATCH http://localhost:8000/issues/12 \
  -H 'Content-Type: application/json' -d '{"state":"open"}'
```

> **On "delete".** GitHub's REST API has no delete-issue operation, so the
> **D** in CRUD is `{"state": "closed"}`. There is deliberately no
> `DELETE /issues/{number}` route: pretending to delete something that still
> exists would be worse than being explicit about the constraint.

### 5. `POST /issues/{number}/comments` — comment

```bash
curl -i -X POST http://localhost:8000/issues/12/comments \
  -H 'Content-Type: application/json' \
  -d '{"body":"Confirmed on staging -- see the attached trace."}'
```

```http
HTTP/1.1 201 Created
Location: /issues/12/comments

{"id":3310028841,"body":"Confirmed on staging -- see the attached trace.",
 "user":{"login":"mikechau1","id":583920,"type":"User","...":"..."},
 "html_url":"https://github.com/mikechau1/cmpe272-issues-gw/issues/12#issuecomment-3310028841",
 "issue_url":"https://api.github.com/repos/mikechau1/cmpe272-issues-gw/issues/12",
 "created_at":"2026-09-06T19:02:44Z","updated_at":"2026-09-06T19:02:44Z"}
```

### 6. `GET /issues/{number}/comments` — list comments

```bash
curl -s 'http://localhost:8000/issues/12/comments?per_page=30&page=1' | jq '.[].body'
```

Paginated the same way as `GET /issues`.

### 7. `POST /webhook` — receive a delivery

Verifies `X-Hub-Signature-256`, persists the delivery, acks `204`, and
interprets it in the background. See [Webhook setup](#webhook-setup).

```bash
# hand-rolled delivery, signed correctly
BODY='{"action":"opened","issue":{"number":12,"title":"hi"},"sender":{"login":"me"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"

curl -i -X POST http://localhost:8000/webhook \
  -H 'Content-Type: application/json' \
  -H 'X-GitHub-Event: issues' \
  -H "X-GitHub-Delivery: $(uuidgen)" \
  -H "X-Hub-Signature-256: $SIG" \
  -d "$BODY"
# HTTP/1.1 204 No Content
```

| Situation | Response |
| --- | --- |
| Valid signature, known event and action | `204 No Content` |
| Same delivery id replayed | `204 No Content`, no new event |
| Missing / wrong / tampered signature | `401 invalid_signature` |
| Unknown event (e.g. `push`) or unknown action | `400` |
| Body over `MAX_WEBHOOK_BODY_BYTES` | `413` |
| `WEBHOOK_SECRET` unset | `503 not_configured` |

`scripts/webhook_replay.sh` does all of this for you, including the tampered
case and a duplicate-delivery check:

```bash
./scripts/webhook_replay.sh                                        # 204, then 204 with no new row
./scripts/webhook_replay.sh tests/fixtures/webhook_ping.json ping  # ping
./scripts/webhook_replay.sh tests/fixtures/webhook_issues_opened.json issues --tamper   # 401
```

### 8. `GET /events` — recent deliveries

```bash
curl -s 'http://localhost:8000/events?limit=10' | jq
curl -s 'http://localhost:8000/events?event=issue_comment' | jq
```

```json
[
  {
    "id": 42,
    "delivery_id": "4f1a2b60-8c3d-11f0-9e21-9b4a2c1d7e55",
    "event": "issue_comment",
    "action": "created",
    "issue_number": 12,
    "repository": "mikechau1/cmpe272-issues-gw",
    "sender": "mikechau1",
    "status": "processed",
    "error": null,
    "attempts": 1,
    "timestamp": "2026-09-06T19:02:45.113Z",
    "processed_at": "2026-09-06T19:02:45.119Z"
  }
]
```

`status` is `received` → `processed`, or `failed` for a poison delivery, in
which case `error` holds the reason.

### 9. `GET /healthz` and `GET /readyz`

```bash
curl -s http://localhost:8000/healthz | jq          # liveness; never calls GitHub
curl -s 'http://localhost:8000/readyz?deep=true' | jq   # also verifies the token + repo
```

`/healthz` is deliberately independent of GitHub, so an upstream outage or an
exhausted rate limit cannot make an orchestrator kill a healthy container.

### Error format

Every failure uses one envelope:

```json
{
  "error": {
    "code": "rate_limited",
    "message": "GitHub primary rate limit exhausted while listing issues (limit 5000/hour). Retry after 1800s. GitHub said: API rate limit exceeded.",
    "status": 429,
    "request_id": "d3d9446802a44259755d38e6d163e820",
    "upstream_status": 403
  }
}
```

`request_id` matches the `X-Request-ID` response header and the `request_id`
field in the logs, so a user-reported failure is one `grep` away.

| Gateway status | `code` | Cause |
| --- | --- | --- |
| 400 | `validation_error` | Bad request body or query, or GitHub returned 422 |
| 401 | `missing_credentials` | `GITHUB_TOKEN` is unset |
| 401 | `github_unauthorized` | GitHub rejected the token |
| 403 | `github_forbidden` | Token lacks `Issues: Read and write`, or issues are disabled |
| 404 | `not_found` / `not_an_issue` | No such issue, invisible to the token, or it's a PR |
| 413 | `payload_too_large` | Webhook body over the limit |
| 429 | `rate_limited` | GitHub rate limit; `Retry-After` is set |
| 502 | `github_unavailable` / `github_unreachable` | GitHub 5xx or network failure |
| 503 | `not_configured` | A required environment variable is unset |
| 504 | `github_timeout` | Upstream exceeded `GITHUB_TIMEOUT_S` |

### Postman / Bruno

Import [`postman/issues-gateway.postman_collection.json`](postman/issues-gateway.postman_collection.json).
Set `baseUrl` and `webhookSecret` (it must match your `.env`), then run
**Create issue** first — it captures `issueNumber` for the later requests. The
webhook requests compute a real HMAC in a pre-request script, so the valid and
tampered cases genuinely return 204 and 401.

---

## Webhook setup

You need a public URL that reaches your local service. Three options:

### Option A — Cloudflare tunnel via docker compose (no account needed)

```bash
docker compose --profile tunnel up --build
docker compose logs tunnel | grep -o 'https://.*trycloudflare.com'
```

### Option B — ngrok

```bash
./scripts/run.sh          # terminal 1
ngrok http 8000           # terminal 2 -> copy the https:// forwarding URL
```

### Option C — smee.io (no tunnel binary)

```bash
npx smee-client --url https://smee.io/YOUR-CHANNEL --target http://localhost:8000/webhook
```

### Then register the webhook

```bash
gh api -X POST "repos/$GITHUB_OWNER/$GITHUB_REPO/hooks" \
  -f name=web \
  -F active=true \
  -f 'events[]=issues' \
  -f 'events[]=issue_comment' \
  -f config[url]="https://YOUR-PUBLIC-URL/webhook" \
  -f config[content_type]=json \
  -f config[secret]="$WEBHOOK_SECRET" \
  -f config[insecure_ssl]=0
```

Or in the UI: **Repository → Settings → Webhooks → Add webhook**

- **Payload URL:** `https://YOUR-PUBLIC-URL/webhook`
- **Content type:** `application/json`
- **Secret:** the exact value of `WEBHOOK_SECRET` — this is the one that trips
  people up; a mismatch shows up as a `401 invalid_signature`
- **Events:** *Let me select individual events* → **Issues** and
  **Issue comments**

GitHub immediately sends a `ping`, which should appear in `/events`.

### Verify it end to end

```bash
curl -s -X POST http://localhost:8000/issues \
  -H 'Content-Type: application/json' -d '{"title":"webhook check"}' | jq .number

sleep 3
curl -s 'http://localhost:8000/events?limit=5' | jq '.[] | {event, action, issue_number, status}'
```

### Redelivering

**Settings → Webhooks → your hook → Recent Deliveries** shows every attempt
with its full request and response. Pick one and click **Redeliver**.

The response stays `204` and `/events` does **not** grow a second row: the
dedupe key is `(X-GitHub-Delivery, event, action)`, so a replay is recognised
and ignored. That is the property to demo — redeliver the same event twice and
show that `/events` is unchanged.

```bash
gh api "repos/$GITHUB_OWNER/$GITHUB_REPO/hooks/HOOK_ID/deliveries"                    # list
gh api -X POST "repos/$GITHUB_OWNER/$GITHUB_REPO/hooks/HOOK_ID/deliveries/ID/attempts" # redeliver
```

> **After the demo, rotate the secret.** Generate a new one
> (`openssl rand -hex 32`), update both `.env` and the webhook config, and
> restart. Tunnel URLs are public while they live.

---

## Running the tests

```bash
make test               # unit + mocked integration -- no credentials needed
make test-unit          # unit tests only
make test-integration   # against the real GitHub API (reads .env)
make test-tunnel        # end-to-end webhooks (needs a tunnel; see above)
make cov                # coverage report -> htmlcov/index.html
make lint               # ruff check + format --check
make spec               # validate openapi.yaml as OpenAPI 3.1
```

Or directly:

```bash
pytest -m "not integration and not tunnel"
pytest -m integration
```

### What is covered

**Unit** (`tests/unit`, no network — `respx` intercepts every outbound call):

| File | Covers |
| --- | --- |
| `test_validation.py` | Missing title → 400, invalid state → 400, `per_page` bounds, unknown fields, empty PATCH — and that a rejected request never reaches GitHub |
| `test_webhook_signature.py` | Valid / invalid / missing / malformed signatures, tampered bodies, wrong secret, ordering (signature before event), ping, unknown event and action, dedupe |
| `test_error_mapping.py` | GitHub 401/403/404/410/422/5xx → our error objects; rate-limit vs. permission-denial disambiguation; timeouts |
| `test_pagination.py` | `Link` parsing (quoted, unquoted, multi-rel, commas in URLs), rebuilding, cursor extraction, `per_page` clamping |
| `test_github_client.py` | Required headers, retry policy, backoff, ETag conditional GET, LRU eviction |
| `test_store.py` | Dedupe keys, ordering, retention, poison parking, restart persistence |
| `test_issues_routes.py` | Status codes, `Location`, header forwarding, PR filtering, projections |
| `test_openapi_contract.py` | Spec validity, examples, and that the contract and the code cannot drift apart |
| `test_logging_and_config.py` | Structured logs, secret redaction, env parsing |

**Integration** (`tests/integration`):

- `test_github_live.py` (`-m integration`) — real API: create → read; update
  title/body; close → reopen; comment → list comments; pagination; a real
  conditional `304`; real 404 and 422 paths. Issues it creates are prefixed
  `[integration]` and closed on teardown.
- `test_resilience.py` (no marker, always runs) — the negative paths you cannot
  provoke on demand: rate-limit exhaustion, secondary limits, 5xx retry and
  give-up, timeouts, poison deliveries. Mocked, but driven through the whole app.
- `test_webhook_e2e.py` (`-m tunnel`) — real deliveries through a public tunnel.

### Current results

```
320 passed   (304 credential-free + 16 live)
coverage: 96% of app/  (target: 80%)
```

---

## Docker

```bash
make docker-build
make docker-run                       # or the raw command below
```

```bash
docker build -t issues-gateway:local .

docker run --rm -it --name issues-gateway \
  -e GITHUB_TOKEN="$GITHUB_TOKEN" \
  -e GITHUB_OWNER="$GITHUB_OWNER" \
  -e GITHUB_REPO="$GITHUB_REPO" \
  -e WEBHOOK_SECRET="$WEBHOOK_SECRET" \
  -e PORT=8000 \
  -e EVENT_STORE_PATH=/data/events.db \
  -p 8000:8000 \
  -v issues-gateway-data:/data \
  issues-gateway:local
```

Or with an env file (never baked into an image layer):

```bash
docker run --rm -it --env-file .env -p 8000:8000 \
  -v issues-gateway-data:/data issues-gateway:local
```

Notes: the image is a two-stage build (no compiler or pip cache in the
runtime layer), runs as uid 10001, and declares a `HEALTHCHECK` that hits
`/healthz` without reaching GitHub. `/data` is a volume so the webhook event
store — and therefore dedupe — survives a restart.

`docker-compose.yaml` adds an optional Cloudflare tunnel under the `tunnel`
profile. A [devcontainer](.devcontainer/devcontainer.json) is included for
VS Code / GitHub Codespaces.

---

## How it works

```
                       ┌──────────────────────────────────────────┐
  client  ──────────►  │  RequestContextMiddleware                │
                       │    request id, structured access log     │
                       ├──────────────────────────────────────────┤
                       │  routers/issues.py     validation, 400s  │
                       │  routers/webhook.py    HMAC, dedupe      │
                       │  routers/health.py     probes            │
                       ├─────────────────┬────────────────────────┤
                       │ github_client   │  store (SQLite)        │
                       │  retry/backoff  │   dedupe key:          │
                       │  rate limits    │   (delivery, event,    │
                       │  ETag cache     │    action)             │
                       └────────┬────────┴────────────────────────┘
                                │
                                ▼
                        api.github.com
```

The short version of the decisions — the full reasoning is in
**[DESIGN.md](DESIGN.md)**:

- **Error mapping.** GitHub overloads `403` for rate limiting *and* permission
  denial, and answers `404` for private resources. Passing those through would
  leave callers unable to tell "wait" from "fix your token", so they map to
  `429` (with `Retry-After`) and `403` respectively.
- **Pagination.** GitHub's cursors are preserved exactly; only the URLs are
  rewritten, to point at this service and carry the caller's filters forward.
- **Webhook dedupe.** `(delivery_id, event, action)` is a unique index, so
  retries and manual redeliveries are absorbed by the database rather than by
  handler logic.
- **Ack fast.** Verify → persist → `204` → interpret in the background. A slow
  consumer can never push past GitHub's 10-second delivery timeout.
- **Retries are asymmetric.** Safe and idempotent methods retry; `POST` does
  not, because a retried create could produce two issues.
- **Security.** Constant-time HMAC comparison, signature verified before the
  body is parsed, secrets never logged or echoed.

---

## Project layout

```
.
├── openapi.yaml                     # OpenAPI 3.1 contract (source of truth)
├── README.md
├── DESIGN.md                        # design note: mapping, pagination, dedupe, trade-offs
├── Dockerfile / docker-compose.yaml / .dockerignore
├── Makefile
├── .env.example                     # .env is git-ignored
├── requirements.txt / requirements-dev.txt / pyproject.toml
├── .devcontainer/devcontainer.json
├── .github/workflows/ci.yml         # lint, contract, test matrix, live tests, docker build
├── app/
│   ├── main.py                      # factory, lifespan, error handlers, contract wiring
│   ├── config.py                    # 12-factor settings
│   ├── logging_config.py            # structured JSON logs, redaction
│   ├── middleware.py                # request id, access log
│   ├── errors.py                    # error envelope + GitHub translation
│   ├── schemas.py                   # request models and upstream projections
│   ├── github_client.py             # auth, retry, rate limits, ETag cache
│   ├── pagination.py                # Link parsing and rewriting
│   ├── security.py                  # HMAC verification
│   ├── store.py                     # SQLite delivery store
│   └── routers/                     # issues.py, webhook.py, health.py
├── tests/
│   ├── conftest.py, fixtures/       # captured GitHub payloads
│   ├── unit/                        # 9 files, no network
│   └── integration/                 # live, resilience, tunnel
├── scripts/
│   ├── run.sh                       # one-click local or docker run
│   ├── smoke.sh                     # exercise every route
│   └── webhook_replay.sh            # signed replay, tamper check, dedupe check
└── postman/issues-gateway.postman_collection.json
```

---

## Make targets

```
install            Create the virtualenv and install dependencies
env                Create .env from the template if it does not exist
run                Run the service (reads .env)
dev                Run with auto-reload for development
test               Run everything that needs no credentials
test-unit          Run the unit tests only
test-integration   Run tests against the real GitHub API
test-tunnel        Run the end-to-end webhook tests
cov                Coverage report
lint / fmt         ruff check / ruff format
spec               Validate openapi.yaml as OpenAPI 3.1
docker-build       Build the container image
docker-run         Run the container image with .env
compose-up         Start with docker compose
compose-tunnel     Start with a public Cloudflare tunnel
clean              Remove build, test and cache artefacts
```
