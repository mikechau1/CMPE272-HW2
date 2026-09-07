# Design note — GitHub Issues Gateway

CMPE 272 HW2. Four decisions worth defending, plus the trade-offs behind them.

## 1. Error mapping

GitHub's status codes are ambiguous in ways that matter to a caller deciding
what to do next. `403` means *three* unrelated things — primary rate limit,
secondary rate limit, or a real permission denial. `404` is returned for
resources that exist but are invisible to the token, because GitHub refuses to
confirm a private repository to an unauthorised caller. Passing these through
verbatim would leave a client unable to distinguish *"wait 30 minutes"* from
*"fix your token's permissions"* — the two commonest failures, both arriving
as `403`.

So the gateway picks a status for what the caller should **do**, and returns
one envelope: `{"error": {code, message, status, request_id, upstream_status,
details}}`.

| GitHub | Discriminator | Gateway | `code` |
| --- | --- | --- | --- |
| 401 | — | 401 | `github_unauthorized` |
| 403 | `x-ratelimit-remaining: 0` or `retry-after` | **429** + `Retry-After` | `rate_limited` |
| 403 | neither | 403 | `github_forbidden` |
| 404 / 410 | — | 404 | `not_found` / `gone` |
| 400 / 422 | — | **400** | `validation_error` |
| 5xx / timeout / connect error | — | **502** / **504** / **502** | `github_unavailable` / `github_timeout` / `github_unreachable` |

Three points behind the table. **`403` → `429` is the important one**:
`is_rate_limited()` checks headers first and message text only as a fallback,
so a permission denial is never mistaken for throttling — the failure mode
that would have a client sleep 30 minutes over a scope it will never be
granted. **Upstream 5xx becomes `502`, not `500`**, reserving our `5xx` for
*our* faults; anything else makes an on-call dashboard lie. And **messages
name the fix**: a rejected token says "check that GITHUB_TOKEN is valid,
unexpired, and scoped to this repository"; a `404` explains that GitHub
answers `404` rather than `403` for private resources, so nobody wastes an
hour on a resource that is actually there. `upstream_status` preserves the raw
truth, and `request_id` appears in the body, the `X-Request-ID` header, and
every log line.

Client validation errors are `400`, not FastAPI's default `422`, because the
contract says `400` — with a `details` array naming each offending field.

## 2. Pagination

**Preserve GitHub's semantics; hide GitHub's URLs.** `page` and `per_page`
pass through (clamped to 1–100) and the `Link` header is parsed, rewritten
against *this* service, and re-emitted with the caller's own filters merged
back in:

```
upstream:  <https://api.github.com/repositories/889012345/issues?state=open&page=2>; rel="next"
returned:  </issues?state=open&labels=bug&page=2&per_page=5>; rel="next"
```

Forwarding GitHub's URLs would hand clients links they cannot call (no token)
and leak the upstream into our contract; dropping the header would force them
to guess when to stop. Only parameters this API actually accepts survive the
rewrite — GitHub has begun adding opaque `after` cursors, and a link carrying
parameters our routes ignore is a link that lies. `parse_link_header` is
deliberately lenient (bad segments are skipped, not raised), because a
malformed upstream header should degrade pagination, not fail the request.

Two upstream quirks are absorbed here rather than pushed onto clients. **Pull
requests are filtered out** — GitHub serves them from the issues endpoint, but
this is an issues API, so a page of 30 may return fewer than 30 rows; the
alternative is every caller filtering PRs forever. And **listings are
eventually consistent**: an issue is readable at `/issues/{n}` immediately but
takes a beat to appear in a filtered list. That is GitHub's behaviour, so it
is documented and the integration tests poll rather than demanding
read-your-writes.

**Conditional GET (extra credit)** works on both hops. Gateway → GitHub:
ETags are cached per URL+query in a bounded LRU and replayed as
`If-None-Match`; a `304` costs *no rate-limit budget*. Client → gateway: we
return our own ETag and honour a matching `If-None-Match`. Ours is versioned —
`W/"gw1-<upstream>"` — because reusing GitHub's tag unmodified would be a real
bug: a client holding a tag from an older deploy would get a `304` and keep
rendering the old shape. The cost is that a `304` means "GitHub's cached view
is unchanged", widening the consistency window above; `ENABLE_ETAG_CACHE=false`
turns it off.

## 3. Webhook dedupe

GitHub retries any delivery it cannot confirm, and "Redeliver" exists so
operators can replay one. At-least-once is guaranteed; exactly-once is not.
Idempotency has to live somewhere — and the right place is the database, not
handler logic:

```sql
CREATE UNIQUE INDEX ux_webhook_dedupe ON webhook_events (delivery_id, event, action);
```

`INSERT … ON CONFLICT DO NOTHING` turns a replay into a no-op that still acks
`204`. A duplicate is *not* an error: answering `409` would make GitHub keep
retrying and eventually disable the hook. Two details that took thought —
`action` is stored as `''`, never `NULL`, because SQLite treats `NULL`s as
distinct inside a unique index and every `ping` replay would slip through; and
a missing `X-GitHub-Delivery` falls back to a SHA-256 of the body, so
hand-rolled replays still dedupe deterministically.

The order is **verify HMAC → validate event/action → persist → 204 →
interpret in background**. The signature is checked *before the body is
parsed*, because until the HMAC verifies the body is attacker-controlled
input. The event check comes second so an unauthenticated caller cannot probe
which events we handle — an unsigned request with a bogus event gets `401`,
never `400`. Persist-then-ack, because a crash between them loses the delivery
and GitHub allows only 10 seconds; the SQLite insert takes microseconds and
runs off the event loop.

**Poison handling:** a background task that raises marks its row `failed` with
the error and stops. It is not retried in-band and not reported to GitHub —
the delivery *was* accepted, and re-raising would make GitHub retry a payload
that fails identically every time until the hook is disabled. Failures stay
visible at `GET /events`. This is a dead-letter queue with the queue collapsed
into a status column.

## 4. Security trade-offs

**Taken.** Constant-time HMAC comparison (`hmac.compare_digest`) — a naive
`==` short-circuits on the first mismatched byte, enough to forge a digest one
byte at a time. Verify before parse. Fail closed: with `WEBHOOK_SECRET` unset,
`/webhook` returns `503` rather than accepting unsigned traffic. Secrets never
reach a log record — `redact()` renders a length, never a value, and tests
assert the secret and signature are absent from logs *and* error bodies.
Rejected signatures give a vague reason, so nothing helps an attacker iterate.
Inputs are bounded (request ids truncated, bodies capped at `413`, `per_page`
clamped). Least privilege: a fine-grained PAT with `Issues: Read and write` on
one repository, where a classic `repo` PAT would also grant code, Actions and
secrets. The container runs unprivileged.

**Not taken, deliberately.** *No client auth on our own API* — it is meant for
localhost or behind an existing gateway; in production it needs auth in front,
and that assumption is stated in the contract rather than left implicit. *No
replay-window check*: GitHub does not sign a timestamp, so a captured,
never-delivered payload would be accepted. The dedupe index blunts replays of
*seen* deliveries; fixing it properly needs mTLS or GitHub's hook IP
allowlist. *Rate-limit state is per-process*, so replicas share GitHub's
budget without coordinating — the correct fix is a shared token bucket, but
the failure mode is graceful (each replica sees the `429` and backs off).

## 5. Two more decisions

**Retries are asymmetric.** Safe and idempotent methods retry on 5xx, `429`
and transport errors with capped exponential backoff. `POST` **never** does: a
create that timed out may have succeeded, and retrying would file the issue
twice. Losing one write to a visible `502` beats silently duplicating it.

**The contract is the source of truth.** `openapi.yaml` is hand-written and
served *as the app's schema*, so `/docs` shows the contract, not a schema
reverse-engineered from decorators. Nothing forces the two to agree, so
`test_openapi_contract.py` supplies the force: every implemented route must be
documented, every documented route must exist, every `$ref` must resolve.
Generating the spec from code would guarantee agreement but demote the
contract to a *report* on the implementation rather than a commitment it must
meet.
