# GitHub Issues Gateway

CMPE 272 — Homework 2

A small, production-shaped HTTP service that fronts the **GitHub Issues REST API**
for one repository and ingests that repository's **webhooks**.

- Contract-first: [`openapi.yaml`](openapi.yaml) (OpenAPI 3.1) is the source of
  truth and is what the service actually serves at `/openapi.json` and `/docs`.
- HMAC-SHA256 webhook verification with constant-time comparison, and a
  delivery store that makes redelivery a no-op.
- Rate-limit aware, paginating, retry-aware GitHub client with conditional GET.
- 322 automated tests; 96% line coverage; 16 of them run against real GitHub.

**Stack:** Python 3.11+ / **FastAPI** / httpx / SQLite.
**Testing:** pytest + respx. **API examples:** HTTPie. **Tunnel:** ngrok.

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
git clone https://github.com/mikechau1/CMPE272-HW2.git
cd CMPE272-HW2

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

To exercise every route against your real repository in one go, with HTTPie:

```bash
make examples          # or: ./scripts/httpie_examples.sh
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

> **Two repositories are in play, on purpose.**
>
> | Repository | Role |
> | --- | --- |
> | [`mikechau1/CMPE272-HW2`](https://github.com/mikechau1/CMPE272-HW2) | This source tree — the submission |
> | [`mikechau1/cmpe272-issues-gw`](https://github.com/mikechau1/cmpe272-issues-gw) | The repository the gateway *operates on*: `GITHUB_OWNER`/`GITHUB_REPO`, where issues get created and webhooks are registered |
>
> They are separate because the integration tests create and close real issues
> and the webhook fires on every one of them. Keeping that out of the
> submission repo means the graded history stays readable. Nothing stops you
> pointing `GITHUB_REPO` at a single repo instead — the service does not care.

### 1. A dedicated test repository

```bash
gh repo create cmpe272-issues-gw --private \
  --description "CMPE 272 HW2 - issues + webhook target for the gateway"
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

Base URL: `http://localhost:${PORT}`. Every example is HTTPie and is
copy-pasteable as-is. `make install` puts HTTPie in `.venv/bin`, so no
system-wide install is needed:

```bash
make install                       # provides .venv/bin/http
export PATH="$PWD/.venv/bin:$PATH" # or: pip install httpie
```

HTTPie shorthand used throughout: `:8000/issues` means
`http://localhost:8000/issues`; `key=value` is a JSON body field;
`key:=value` is a raw JSON value (numbers, arrays, objects); `key==value` is a
query parameter; `Header:value` is a request header.

Clients of this API are unauthenticated — it is meant to run on localhost or
behind your own gateway. The service holds the GitHub credential; it never
accepts one from the caller.

> **Run every example at once.** `./scripts/httpie_examples.sh` drives all of
> the below against a running service and asserts the expected status for each
> — 26 checks including the webhook signature and idempotency cases. It is the
> runnable equivalent of an API collection.

### 1. `POST /issues` — create

```bash
http POST :8000/issues \
  title="Rate limiter drops the Retry-After header" \
  body="Reproduced on main at 0f21ac9." \
  labels:='["bug", "gateway"]'
```

```http
HTTP/1.1 201 Created
Location: /issues/12
X-Request-ID: 8f14e45fceea167a5a36dedd4bea2543
X-RateLimit-Remaining: 4993

{
    "number": 12,
    "id": 2451900001,
    "title": "Rate limiter drops the Retry-After header",
    "body": "Reproduced on main at 0f21ac9.",
    "state": "open",
    "state_reason": null,
    "labels": [{"name": "bug", "color": "d73a4a", "description": "Something isn't working"}],
    "user": {"login": "mikechau1", "id": 583920, "type": "User", "...": "..."},
    "assignees": [],
    "comments": 0,
    "locked": false,
    "html_url": "https://github.com/mikechau1/cmpe272-issues-gw/issues/12",
    "created_at": "2026-09-06T18:20:11Z",
    "updated_at": "2026-09-06T18:20:11Z",
    "closed_at": null
}
```

Title only:

```bash
http POST :8000/issues title="Something is broken"
```

Invalid payload → `400` (not FastAPI's default 422; the contract specifies 400):

```bash
http POST :8000/issues body="no title supplied"
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
http GET :8000/issues state==open labels==bug page==1 per_page==5
```

Add `--print=hb` (or `-v` for the request too) to see the pagination headers:

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

The `Link` header is GitHub's pagination **rewritten to point at this service**
and carrying your filters forward, so you can follow `rel="next"` directly.
Only parameters this API accepts survive the rewrite. Pull requests are
filtered out — GitHub serves them from the same endpoint, but this is an issues
API.

**Conditional GET (extra credit).** Send the `ETag` back and get a `304` that
costs no GitHub rate-limit budget:

```bash
ETAG=$(http --print=h GET :8000/issues per_page==5 \
       | awk 'tolower($1)=="etag:"{print $2}' | tr -d '\r')

http --print=h GET :8000/issues per_page==5 "If-None-Match:${ETAG}"
# HTTP/1.1 304 Not Modified
```

### 3. `GET /issues/{number}` — read one

```bash
http GET :8000/issues/12
http GET :8000/issues/99999999      # 404
```

### 4. `PATCH /issues/{number}` — update, close, reopen

Only the keys you send are forwarded, so a partial update cannot clobber
fields you did not mention.

```bash
# rename
http PATCH :8000/issues/12 title="Rate limiter drops Retry-After (confirmed)"

# edit the body
http PATCH :8000/issues/12 body="Updated repro steps."

# close -- this API's DELETE
http PATCH :8000/issues/12 state=closed state_reason=completed

# reopen
http PATCH :8000/issues/12 state=open

# rejected: not a valid state
http PATCH :8000/issues/12 state=deleted     # 400
```

> **On "delete".** GitHub's REST API has no delete-issue operation, so the
> **D** in CRUD is `{"state": "closed"}`. There is deliberately no
> `DELETE /issues/{number}` route: pretending to delete something that still
> exists would be worse than being explicit about the constraint.

### 5. `POST /issues/{number}/comments` — comment

```bash
http POST :8000/issues/12/comments body="Confirmed on staging -- see the attached trace."
```

```http
HTTP/1.1 201 Created
Location: /issues/12/comments

{
    "id": 3310028841,
    "body": "Confirmed on staging -- see the attached trace.",
    "user": {"login": "mikechau1", "id": 583920, "type": "User", "...": "..."},
    "html_url": "https://github.com/mikechau1/cmpe272-issues-gw/issues/12#issuecomment-3310028841",
    "issue_url": "https://api.github.com/repos/mikechau1/cmpe272-issues-gw/issues/12",
    "created_at": "2026-09-06T19:02:44Z",
    "updated_at": "2026-09-06T19:02:44Z"
}
```

### 6. `GET /issues/{number}/comments` — list comments

```bash
http GET :8000/issues/12/comments per_page==30 page==1
```

Paginated the same way as `GET /issues`.

### 7. `POST /webhook` — receive a delivery

Verifies `X-Hub-Signature-256`, persists the delivery, acks `204`, and
interprets it in the background. See [Webhook setup](#webhook-setup).

To hand-roll a delivery, sign the exact bytes you are about to send — GitHub
signs the raw body, so the digest and the transmitted payload must not differ
by even a newline. `printf '%s'` (no trailing newline) piped into HTTPie sends
stdin verbatim:

```bash
BODY='{"action":"opened","issue":{"number":12,"title":"hi"},"sender":{"login":"me"}}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$WEBHOOK_SECRET" | awk '{print $NF}')"

printf '%s' "$BODY" | http POST :8000/webhook \
  Content-Type:application/json \
  X-GitHub-Event:issues \
  X-GitHub-Delivery:"$(uuidgen)" \
  X-Hub-Signature-256:"$SIG"
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

`scripts/webhook_replay.sh` replays a captured payload for you, including the
tampered case and a duplicate-delivery check:

```bash
./scripts/webhook_replay.sh                                        # 204, then 204 with no new row
./scripts/webhook_replay.sh tests/fixtures/webhook_ping.json ping  # ping
./scripts/webhook_replay.sh tests/fixtures/webhook_issues_opened.json issues --tamper   # 401
```

### 8. `GET /events` — recent deliveries

```bash
http GET :8000/events limit==10
http GET :8000/events event==issue_comment
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
http GET :8000/healthz             # liveness; never calls GitHub
http GET :8000/readyz deep==true   # also verifies the token can see the repo
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

### Useful HTTPie flags

| Flag | Why |
| --- | --- |
| `-v` / `--verbose` | Print the request as well as the response |
| `--print=h` | Response headers only — how to see `Link`, `ETag`, `X-Page` |
| `--print=hb` | Headers and body |
| `--check-status` | Exit non-zero on 4xx/5xx, for use in scripts |
| `--offline` | Build and print the request without sending it |
| `--ignore-stdin` | Required in scripts and pipelines, so HTTPie does not wait on stdin |
| `--pretty=format` | Keep JSON indented even when piping to another command |

---

## Webhook setup

GitHub needs a public HTTPS URL to deliver to. This project uses **ngrok**.

### 1. Install and authenticate ngrok

```bash
brew install ngrok                      # or https://ngrok.com/download
ngrok config add-authtoken <YOUR_TOKEN> # free account, one time
```

The authtoken is at
<https://dashboard.ngrok.com/get-started/your-authtoken>.

### 2. Start the service, then the tunnel

```bash
./scripts/run.sh        # terminal 1 -- gateway on :8000
ngrok http 8000         # terminal 2  (or: make tunnel)
```

ngrok prints a forwarding URL. Copy the `https://` one:

```
Forwarding   https://a1b2-c3d4-e5f6.ngrok-free.app -> http://localhost:8000
```

Prefer containers? `docker compose --profile tunnel up --build` runs both,
reading `NGROK_AUTHTOKEN` from `.env`. Get the URL from the local API:

```bash
http --ignore-stdin GET :4040/api/tunnels | \
  python3 -c "import json,sys; print(json.load(sys.stdin)['tunnels'][0]['public_url'])"
```

> On the free plan the URL changes every time ngrok restarts, and the webhook
> config has to be updated to match. A reserved domain (`ngrok http
> --url=your-domain.ngrok-free.app 8000`) avoids that.

### 3. Register the webhook

```bash
NGROK_URL=https://a1b2-c3d4-e5f6.ngrok-free.app

gh api -X POST "repos/$GITHUB_OWNER/$GITHUB_REPO/hooks" \
  -f name=web \
  -F active=true \
  -f 'events[]=issues' \
  -f 'events[]=issue_comment' \
  -f config[url]="${NGROK_URL}/webhook" \
  -f config[content_type]=json \
  -f config[secret]="$WEBHOOK_SECRET" \
  -f config[insecure_ssl]=0
```

Or in the UI: **Repository → Settings → Webhooks → Add webhook**

- **Payload URL:** `https://YOUR-NGROK-URL/webhook`
- **Content type:** `application/json`
- **Secret:** the exact value of `WEBHOOK_SECRET` — this is the one that trips
  people up; a mismatch shows up as `401 invalid_signature`
- **Events:** *Let me select individual events* → **Issues** and
  **Issue comments**

GitHub immediately sends a `ping`, which should appear in `/events`.

### 4. Verify it end to end

```bash
http POST :8000/issues title="webhook check"

sleep 3
http GET :8000/events limit==5
```

You should see an `issues` / `opened` row with `status: processed`.

**ngrok's inspector is the best debugging tool here.** <http://localhost:4040>
shows every delivery GitHub sent, with full request and response bodies — so
you can see the exact payload, the signature header, and what the gateway
answered. It also has a **Replay** button, which re-sends a delivery with the
same headers: a one-click way to watch the idempotency logic absorb a
duplicate. Replay any delivery and confirm `/events` does not grow.

### 5. Redelivering from GitHub

**Settings → Webhooks → your hook → Recent Deliveries** lists every attempt
with its full request and response. Pick one and click **Redeliver**.

The response stays `204` and `/events` does **not** grow a second row: the
dedupe key is `(X-GitHub-Delivery, event, action)`, so a replay is recognised
and ignored. That is the property to demo — redeliver the same event twice and
show `/events` unchanged.

```bash
HOOK_ID=$(gh api "repos/$GITHUB_OWNER/$GITHUB_REPO/hooks" -q '.[0].id')
gh api "repos/$GITHUB_OWNER/$GITHUB_REPO/hooks/$HOOK_ID/deliveries"
gh api -X POST "repos/$GITHUB_OWNER/$GITHUB_REPO/hooks/$HOOK_ID/deliveries/ID/attempts"
```

### 6. Run the end-to-end webhook tests

With the tunnel up and the webhook registered:

```bash
RUN_TUNNEL_TESTS=1 make test-tunnel
```

These create real issues and comments, then poll `/events` until the matching
delivery arrives.

> **After the demo, rotate the secret.** Generate a new one
> (`openssl rand -hex 32`), update both `.env` and the webhook config, and
> restart. ngrok URLs are public while they live, and the free-plan URL is
> guessable enough to be worth not leaving pointed at a stale secret.

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
322 passed   (306 credential-free + 16 live)
coverage: 96% of app/  (target: 80%)
```

Unit tests are hermetic — they construct `Settings(_env_file=None)`, so the
suite behaves identically with and without a local `.env`.

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs lint, OpenAPI
validation, the credential-free suite on Python 3.11/3.12/3.13, a Docker build
that boots the image and probes it, and the live integration tests.

The live job is opt-in: it needs a repository secret named `GH_ISSUES_TOKEN`
(a fine-grained PAT with `Issues: Read and write`). Without it the job logs a
notice and passes, so CI stays green on a fork.

```bash
gh secret set GH_ISSUES_TOKEN --repo "$GITHUB_OWNER/$GITHUB_REPO"
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

`docker-compose.yaml` adds an optional ngrok tunnel under the `tunnel` profile
(`docker compose --profile tunnel up`), which needs `NGROK_AUTHTOKEN` in
`.env` and exposes ngrok's inspector on <http://localhost:4040>. A
[devcontainer](.devcontainer/devcontainer.json) is included for VS Code /
GitHub Codespaces.

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
└── scripts/
    ├── run.sh                       # one-click local or docker run
    ├── httpie_examples.sh           # every route in HTTPie, 26 asserted checks
    └── webhook_replay.sh            # signed replay, tamper check, dedupe check
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
examples           Exercise every route with HTTPie against a running service
tunnel             Expose the local service to GitHub with ngrok
docker-build       Build the container image
docker-run         Run the container image with .env
compose-up         Start with docker compose
compose-tunnel     Start with docker compose plus ngrok
clean              Remove build, test and cache artefacts
```
