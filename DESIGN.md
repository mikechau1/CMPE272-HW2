# Design note — GitHub Issues Gateway

CMPE 272 HW2. Four decisions worth defending: how upstream errors are
translated, how pagination is preserved, how webhook deliveries are
de-duplicated, and what security is traded for what.

---

## 1. Error mapping

**The problem.** GitHub's status codes are ambiguous in ways that matter to a
caller deciding what to do next:

- `403` means *three* unrelated things — primary rate limit, secondary rate
  limit, or a genuine permission denial.
- `404` is returned for resources that exist but are invisible to the token,
  because GitHub deliberately refuses to confirm a private repository's
  existence to an unauthorised caller.
- `422` is a validation failure, which is the caller's fault, not a server's.

Passing these through verbatim would leave a client unable to distinguish
*"wait 30 minutes"* from *"fix your token's permissions"* — the two most
common failure modes, both arriving as `403`.

**The rule.** One envelope, and a status chosen for what the caller should
*do*:

```json
{"error": {"code": "...", "message": "...", "status": 429,
           "request_id": "...", "upstream_status": 403, "details": [...]}}
```

| GitHub | Discriminator | Gateway | `code` |
| --- | --- | --- | --- |
| 401 | — | 401 | `github_unauthorized` |
| 403 | `x-ratelimit-remaining: 0` | **429** + `Retry-After` | `rate_limited` |
| 403 | `retry-after` present | **429** + `Retry-After` | `rate_limited` |
| 403 | neither | 403 | `github_forbidden` |
| 404 | — | 404 | `not_found` |
| 410 | — | 404 | `gone` |
| 400 / 422 | — | **400** | `validation_error` |
| 5xx | — | **502** | `github_unavailable` |
| timeout | — | **504** | `github_timeout` |
| connect error | — | **502** | `github_unreachable` |

Three points behind the table:

- **`403` → `429` is the important one.** `is_rate_limited()` checks the
  headers first and the message text only as a fallback, so a permission
  denial is never mistaken for throttling — the failure mode that would have a
  client sleep 30 minutes over a scope it will never be granted.
- **Upstream 5xx becomes `502`, not `500`.** Our `5xx` is reserved for *our*
  faults. A `500` from this service means a bug here; a `502` means GitHub
  failed. Anything else makes an on-call dashboard lie.
- **Messages name the fix, not just the fault.** A rejected token says
  *"Check that GITHUB_TOKEN is valid, unexpired, and scoped to this
  repository"*; a `404` explains that GitHub returns `404` rather than `403`
  for private resources, so nobody wastes an hour on a resource that is
  actually there. `upstream_status` is preserved for anyone who needs the raw
  truth.

Validation errors are `400`, not FastAPI's default `422`, because the contract
says `400` — with a `details` array naming each offending field. The
`request_id` in every error body matches the `X-Request-ID` header and the
`request_id` field on every log line.

---

## 2. Pagination

**Preserve GitHub's semantics; hide GitHub's URLs.**

`page` and `per_page` pass straight through (clamped to GitHub's 1–100), and
the `Link` header is parsed, rewritten, and re-emitted. Every rel is rebuilt
against *this* service, and the caller's own filters (`state`, `labels`,
`sort`, `direction`) are merged back in:

```
upstream:  <https://api.github.com/repositories/889012345/issues?state=open&page=2>; rel="next"
returned:  </issues?state=open&labels=bug&page=2&per_page=5>; rel="next"
```

Forwarding GitHub's URLs verbatim would hand clients links they cannot call
(no token) and leak an implementation detail into the public contract. Losing
the header instead would force clients to guess when to stop paging. `X-Page`
and `X-Per-Page` echo what was actually applied after clamping.

`parse_link_header` is deliberately lenient — unparseable segments are skipped
rather than raised — because a malformed upstream header should degrade
pagination, not fail the request. It handles the cases that break naive
regexes: unquoted `rel`, multi-token `rel="next alternate"`, and commas inside
URLs (`labels=bug,gateway`).

Two upstream quirks are absorbed here rather than pushed onto clients:

- **Pull requests are filtered out.** `GET /repos/{o}/{r}/issues` returns PRs
  too. This is an *issues* API, so items carrying a `pull_request` key are
  dropped from listings and `GET /issues/{n}` answers `404 not_an_issue`. The
  cost is that a page of 30 can return fewer than 30 rows; the alternative —
  callers filtering PRs themselves, forever — is worse.
- **Listings are eventually consistent.** An issue is readable at
  `/issues/{number}` immediately but may take seconds to appear in a filtered
  listing. This is GitHub's behaviour, not something a gateway can fix, so it
  is documented and the integration tests poll rather than demanding
  read-your-writes.

### Conditional GET (extra credit)

Two layers, and they compose:

1. **Gateway → GitHub.** `ETag`s are cached per URL+query in a bounded LRU and
   replayed as `If-None-Match`. GitHub answers `304`, we serve the cached body,
   and **the call costs no rate-limit budget** — GitHub does not decrement the
   remaining count for a `304`. `X-Cache: HIT|MISS` reports which happened.
2. **Client → gateway.** We return an `ETag` of our own, and a matching
   `If-None-Match` gets a `304`.

Our `ETag` is derived from GitHub's but versioned: `W/"gw1-<upstream>"`. The
prefix is a projection version, bumped whenever the response shape changes.
Reusing GitHub's tag unmodified would be a real bug — a client holding a tag
from an older deploy would get a `304` and keep rendering the *old* shape.

The trade-off: a `304` means "GitHub's cached view is unchanged", which widens
the eventual-consistency window above. Worth it — a repository under active
polling spends close to zero quota — and `ENABLE_ETAG_CACHE=false` turns it off.

---

## 3. Webhook dedupe and delivery handling

**The problem.** GitHub retries any delivery it cannot confirm, and the
"Redeliver" button exists precisely so operators can replay one. At-least-once
delivery is guaranteed; exactly-once is not. Idempotency has to live
*somewhere*.

**The decision: put it in the database, not the handler.**

```sql
CREATE UNIQUE INDEX ux_webhook_dedupe
    ON webhook_events (delivery_id, event, action);
```

`INSERT … ON CONFLICT DO NOTHING` turns a replay into a no-op that still acks
`204`. A duplicate is *not* an error — answering `409` would make GitHub keep
retrying and eventually disable the hook.

Three details that took thought:

- **The key is `(delivery_id, event, action)`, not `delivery_id` alone.** One
  GUID can legitimately carry different actions in edge cases, and using the
  triple costs nothing.
- **`action` is stored as `''`, never `NULL`.** SQLite treats `NULL`s as
  distinct inside a unique index, so a `NULL` action would let every `ping`
  replay through. This is the kind of bug that only shows up in the demo.
- **The delivery id has a fallback.** If `X-GitHub-Delivery` is absent (a
  hand-rolled `curl`), a SHA-256 of the body is substituted, so identical
  replays still dedupe deterministically.

### Order of operations

```
verify HMAC ──► validate event/action ──► persist ──► 204 ──► interpret (background)
    401              400                  durable    ack        never blocks
```

The signature is checked **before the body is parsed** — until the HMAC
verifies, the body is attacker-controlled input, and nothing downstream should
touch it. The event check comes second so an unauthenticated caller cannot
probe which events we handle: an unsigned request with a bogus event gets
`401`, never `400`.

Persist-then-ack, not ack-then-persist: a crash between the two would lose the
delivery, and GitHub allows 10 seconds. A SQLite insert is microseconds, and it
runs in a worker thread so the event loop stays free. Interpretation happens in
a background task, after the ack.

### Poison handling

A background task that raises marks its row `failed` with the error text and
stops. It is **not** retried in-band and **not** reported to GitHub, because
the delivery *was* accepted — re-raising would make GitHub retry a payload
that will fail identically every time, and enough of those disable the hook.
Failures are visible at `GET /events` for inspection and manual replay. This is
a dead-letter queue with the queue collapsed into a status column.

The store is bounded (`EVENT_RETENTION`, default 500) and pruned on insert. It
is a debugging aid, not a system of record; if it were the latter it would need
a real queue and a real retention policy.

---

## 4. Security trade-offs

**Taken:**

- **Constant-time HMAC comparison** (`hmac.compare_digest`). A naive `==`
  short-circuits on the first mismatched byte, which is enough to forge a
  digest one byte at a time. Both operands are fixed-length hex, so
  `compare_digest`'s length-leak caveat does not apply.
- **Verify before parse**, as above.
- **Reject rather than trust.** With `WEBHOOK_SECRET` unset, `/webhook`
  returns `503` instead of accepting unsigned traffic. A misconfigured deploy
  fails closed.
- **Secrets never reach a log record.** `redact()` renders a length, never a
  value. Startup logs `<redacted:93chars>`; a rejected delivery logs
  `signature_present: true` and never the signature. Tests assert the secret
  and the signature are absent from both log output and error bodies.
- **Vague auth failures.** A rejected signature says only that the HMAC did
  not match — not which part failed. Nothing in the response helps an attacker
  iterate.
- **Bounded inputs.** Request ids truncated to 128 characters, webhook bodies
  capped (`413`), `per_page` clamped, titles and bodies length-limited. An
  unbounded field is an unbounded log line and an unbounded memory allocation.
- **Least privilege.** A fine-grained PAT with `Issues: Read and write` on one
  repository. A classic `repo` PAT would work and would also grant code,
  Actions, and secrets.
- **Runs unprivileged** (uid 10001, no shell) and reads all config from the
  environment. `.env` is git-ignored; nothing is baked into an image layer.

**Deliberately not taken, and why:**

- **No client authentication on our own API.** This service is meant to run on
  localhost or behind an existing gateway. In production it would need its own
  auth in front — this is the assumption most worth flagging, and it is stated
  in the OpenAPI description rather than left implicit.
- **No replay-window check on webhook timestamps.** GitHub does not sign a
  timestamp, so a captured delivery could be replayed indefinitely by anyone
  who has one. The dedupe index blunts this — a replay of a *seen* delivery is
  a no-op — but a never-delivered captured payload would be accepted. Fixing
  this properly needs mutual TLS or an allowlist of GitHub's hook IP ranges.
- **No `Retry-After` on our own 503s.** Configuration errors are not transient;
  telling a client to retry would be a lie.
- **Rate-limit state is per-process.** Several replicas share GitHub's budget
  without coordinating. Correct fix: a shared token bucket in Redis. Not worth
  it at this scale, and the failure mode is graceful — each replica sees the
  `429` and backs off independently.

---

## 5. Other decisions worth naming

**Retries are asymmetric.** `GET`, `PATCH`, `PUT` and `DELETE` retry on 5xx,
`429`, and transport errors with exponential backoff (0.25s → 8s, honouring
`Retry-After` up to a 30s cap). `POST` **never** retries: a create that timed
out may well have succeeded, and retrying it would file the same issue twice.
Losing one write to a `502` the caller can see is strictly better than silently
creating a duplicate. The cap on `Retry-After` matters too — an upstream
answering `retry-after: 99999` should not pin a worker for a day.

**The contract is the source of truth.** `openapi.yaml` is hand-written and
served *as the app's schema* — `/docs` renders the contract, not a schema
reverse-engineered from decorators. Nothing forces the two to agree, so
`tests/unit/test_openapi_contract.py` supplies the force: every implemented
route must be documented, every documented route must exist, every `$ref` must
resolve, and every example must satisfy the schema it hangs from. Generating
the spec from the code instead would guarantee agreement but would make the
contract a *report* on the implementation rather than a commitment the
implementation has to meet.

**Responses are projections, not passthroughs.** `project_issue` picks the
documented fields explicitly. Echoing GitHub's payload would be less code and
would silently widen our contract every time GitHub adds a field — and would
break clients when GitHub removed one. A test asserts `node_id` does not leak.

**The health check does not call GitHub.** `/healthz` reports process
liveness only; `/readyz?deep=true` is the opt-in probe that spends a real API
call. If liveness depended on GitHub, an upstream outage or an exhausted rate
limit would make Kubernetes kill a perfectly healthy container — turning a
degradation into an outage.

**SQLite, not Postgres.** The event store needs durability across restarts and
a unique index. That is all. SQLite in WAL mode delivers both with zero
operational surface. The `EventStore` interface is narrow enough
(`record`/`mark_processed`/`mark_failed`/`list_recent`) that swapping the
backend is a contained change.
