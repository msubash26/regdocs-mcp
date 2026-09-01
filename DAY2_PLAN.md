# Day 2 — MCP server v2 (production shape)

**Repo:** `regdocs-mcp` · **Target:** spec `2026-07-28`, SDK `mcp>=2.1,<3`
**Budget:** ~4.5h build + 1h write-up · **Date:** 2026-09-02 (planned 2026-09-01)

---

## Context

Day 1 shipped four tools over 463 MAS documents on stdio: 59 tests, ADR-002..005, green CI,
live in Claude Code and MCP Inspector. Day 2 adds the second transport and authorization.

### Four of Day 2's checklist items already landed on Day 1

The prep plan lists six items for today. These are done:

- [x] **Structured tool outputs rather than stringified JSON** — all four tools declare an
      `outputSchema` and return `structuredContent`, with the serialised JSON kept in a text
      block for older clients.
- [x] **pytest covering the JSON-RPC surface, not just business logic** — 59 tests over
      schemas, annotations, cursor round-trips, `isError` vs protocol errors.
- [x] **README with a 30-second quickstart and an architecture diagram**.
- [x] **Public on GitHub with green CI**.

Pulling them forward cost almost nothing with the v2 SDK. What remains is the transport and
the auth — genuinely a lighter day, but a *differently shaped* one than the prep plan
describes.

### The prep plan's Day 2 is two revisions stale

The plan describes Streamable HTTP as it existed in `2025-03-26`..`2025-11-25`. Revision
`2026-07-28` changed it substantially — the spec page carries an explicit breaking-change
notice. Verified at `modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http`
on 2026-09-01:

| The prep plan says | `2026-07-28` actually requires |
|---|---|
| "single endpoint handling POST/GET/DELETE" | **POST only.** GET and DELETE → `405 Method Not Allowed` with `Allow: POST` |
| "`Mcp-Session-Id` session management" | **Protocol-level sessions removed.** Ignore the header; do not mint or echo session IDs |
| streams resumable via `Last-Event-ID` | **Not resumable.** Ignore the header |
| server→client requests on the SSE stream | Replaced by **MRTR**: return an `InputRequiredResult`, client retries with `inputResponses` |
| standalone GET stream for notifications | Long-lived notifications come from a `subscriptions/listen` response stream |
| (not mentioned) | **New required headers** `Mcp-Method` on every request, `Mcp-Name` on `tools/call` / `resources/read` / `prompts/get` |
| (not mentioned) | Header values **MUST** be validated against the body → `400` + JSON-RPC `-32020 HeaderMismatch` on mismatch |
| (not mentioned) | `Origin` **MUST** be validated → `403` (DNS-rebinding defence); bind localhost only |
| (not mentioned) | Unknown method → `404` + `-32601`, to distinguish from a legacy server's bare 404 |

So the interesting work today is not "implement sessions" — that concept was deleted — but
**implementing statelessness correctly**, and being able to say why the spec removed it.
That inversion is the day's headline talking point.

### Auth is stricter than "a token check"

The prep plan says "a token check plus a written note on the OAuth flow is enough for a
portfolio piece." Under `2026-07-28`, authorization is **optional overall**, but a server
that supports it **MUST**:

- implement **RFC 9728 Protected Resource Metadata** at
  `/.well-known/oauth-protected-resource/mcp`
- answer an unauthenticated request with `401` +
  `WWW-Authenticate: Bearer resource_metadata="...", scope="..."`
- **validate the token audience** (RFC 8707) — accept only tokens minted *for this server*,
  and never accept or forward any other token
- answer insufficient scope with `403` + `error="insufficient_scope"` and the scopes needed

A bare `if token == SECRET` is not that. The achievable and defensible target is to
implement the **resource-server half properly** and be explicit that the authorization
server is out of scope — which is the honest shape of the claim anyway.

### What the SDK already provides (checked, not assumed)

| Need | SDK support |
|---|---|
| Streamable HTTP | `mcp.run(transport="streamable-http")` / `run_streamable_http_async(host="127.0.0.1", port=8000, streamable_http_path="/mcp", stateless_http, transport_security=...)` |
| ASGI app for testing | `MCPServer.streamable_http_app()` |
| 405 on GET/DELETE | `_streamable_http_modern.py` already returns `405` with `Allow: POST` |
| Origin / Host validation | `TransportSecuritySettings(allowed_hosts=[...], allowed_origins=[...])` + `TransportSecurityMiddleware` |
| RFC 9728 PRM endpoint | `mcp/server/auth/routes.py` builds `/.well-known/oauth-protected-resource{path}` |
| Bearer auth | `AuthSettings(issuer_url, resource_server_url, required_scopes, ...)` + a `TokenVerifier` protocol — implement one async method, `verify_token(token) -> AccessToken | None` |
| Body size limit | `max_request_body_size` (4 MB default) |

Defaults already bind `127.0.0.1`, matching the spec's SHOULD. Most of today is wiring and
*verifying* the SDK's compliance, not reimplementing it.

---

## Phase 0 — Housekeeping · 15 min

- [x] `gh auth status` — already on `msubash26`, no switch needed this time
- [x] `./scripts/stack.sh ps` in the copilot — 7 services up 46h, all healthy
- [x] Re-read Day 1's "Outcome" section in `DAY1_PLAN.md`

## Phase 1 — Pin the transport delta · 30 min

Research is already done (table above). Remaining:

- [x] Confirm against the installed SDK which of the `2026-07-28` transport MUSTs it
      actually enforces — specifically `Mcp-Method` / `Mcp-Name` presence and the
      header↔body validation producing `-32020`. **This is the gate:** anything the SDK does
      not enforce, we add as middleware, and anything it does enforce we test rather than
      duplicate.
- [x] Draft **ADR-006** — the transport delta and why sessions were removed

### Phase 1 result — the gate is green, and it moved one Phase 2 item

Probed `mcp` 2.1.1 through `httpx2.ASGITransport` rather than reading source. The SDK
enforces **every** transport MUST: `405`+`Allow: POST` on GET/DELETE, `-32020` for a
mismatched, absent or duplicated `Mcp-Method`/`Mcp-Name`/`MCP-Protocol-Version`, `-32602`
for a missing request envelope, `404`+`-32601` for an unknown method, `403`/`421` on a bogus
`Origin`/`Host`. **Nothing to reimplement as middleware** — Phase 2 is the 90-minute shape.

One gap, and it is a configuration default rather than a missing feature: era routing is by
header alone, so a POST that *omits* `MCP-Protocol-Version` falls through to the legacy
stateful transport and the server mints and echoes an `Mcp-Session-Id`. `stateless_http=True`
closes it with no middleware and keeps legacy clients served. Recorded in ADR-006.

Consequences for the phases below:
- Phase 2 adds `stateless_http=True` — now a **correctness** requirement, not a tuning knob.
- Phase 4's `Mcp-Session-Id` test must use a **header-less** request. Probing only the modern
  path passes on a server that mints sessions on every legacy call.

## Phase 2 — Streamable HTTP transport · 90 min

- [x] `regdocs-mcp --transport {stdio,http}`, with `--host`, `--port`, `--path` (default
      `/mcp`). stdio stays the default so Day 1's Claude Code registration keeps working
      untouched.
- [x] `TransportSecuritySettings` wired with a sane local default (`127.0.0.1` host allowlist,
      empty origin allowlist) and a `--allow-origin` flag for deliberate widening
- [x] ~~Any header validation the Phase 1 gate showed missing, added as ASGI middleware~~ —
      **none was missing.** No middleware of our own; the SDK's enforcement is tested in
      Phase 4 instead of duplicated.
- [x] Manual verification against a live server: `POST /mcp` works; `GET /mcp` and
      `DELETE /mcp` return `405` with `Allow: POST`; a bogus `Origin` returns `403`; an
      `Mcp-Session-Id` header is ignored and not echoed; a mismatched `Mcp-Name` returns
      `-32020`

### Phase 2 result

`server.py` gains `transport_security()` and an `http_app()` factory (the factory so Phase 4
drives the ASGI app with no live port), and `main()` grows the transport flags. 59 existing
tests still green, ruff clean.

Verified with `curl` against a live `uv run regdocs-mcp --transport http --port 8077`:

```
POST /mcp tools/list                        -> 200, four tools
GET /mcp                                    -> 405, Allow: POST
DELETE /mcp                                 -> 405, Allow: POST
Origin: http://evil.example                 -> 403
Origin: http://localhost:3000               -> 403   (SDK default would ALLOW this)
Mcp-Name disagrees with params.name         -> 400, -32020
Mcp-Session-Id on a modern request          -> 200, not echoed
header-less legacy initialize               -> 200, not echoed  <- the ADR-006 fix
--allow-origin http://localhost:3000, then Origin: http://localhost:3000 -> 200
                                                  Origin: http://evil.example   -> 403
```

**One deliberate divergence from the SDK.** `streamable_http_app(host="127.0.0.1")`
auto-enables DNS-rebinding protection but seeds the origin allowlist with
`http://localhost:*`. On this box that includes LangFuse on :3000, so any page served there
could drive the server from a browser. We pass settings explicitly and start the origin
allowlist **empty** — non-browser clients (Claude Code, curl) send no `Origin` and are
unaffected, and widening is an explicit `--allow-origin`. Line 5 above is that difference,
measured.

A non-loopback `--host` is not refused (a container needs `0.0.0.0`) but warns on stderr,
since the spec's DNS-rebinding guidance assumes localhost.

## Phase 3 — Authorization, resource-server half · 90 min

- [x] A `TokenVerifier` implementation — signed-JWT verifier (`auth.py::JWTVerifier`)
      checking signature, expiry, issuer, `aud` against this server's canonical URI, and
      `scope`, with the signing key from `$REGDOCS_JWT_SECRET`.
- [x] `AuthSettings(issuer_url=..., resource_server_url=..., required_scopes=["regdocs:read"])`
- [x] Confirm the SDK serves `/.well-known/oauth-protected-resource/mcp` and that the
      document names the issuer and `scopes_supported`
- [x] `401` carries `WWW-Authenticate: Bearer resource_metadata="...", scope="regdocs:read"`
      — the SDK omits `scope`, so `ScopeChallengeMiddleware` adds it
- [x] `403` + `error="insufficient_scope"` when the token is valid but under-scoped
- [x] Auth is **off by default and opt-in via `--auth`**; `--auth` on stdio exits with an
      error rather than being silently ignored.
- [x] Write down what is deliberately *not* implemented — ADR-007

### Phase 3 result

`auth.py`: `JWTVerifier`, `auth_settings()`, `ScopeChallengeMiddleware`, and a dev-only
`mint_token()` exposed as `python -m regdocs_mcp.auth` so the README's curl example is
runnable and the tests can build the failure cases as real tokens. `pyjwt>=2.10` added.

Measured on a live server (`--transport http --port 8079 --auth`):

```
GET /.well-known/oauth-protected-resource/mcp -> 200, resource + issuer + scopes_supported
no token       -> 401 error="invalid_token"      + resource_metadata + scope
valid          -> 200 tools/list
wrong audience -> 401 error="invalid_token"      <- the confused-deputy case
missing scope  -> 403 error="insufficient_scope"
expired        -> 401 error="invalid_token"
bad signature  -> 401 error="invalid_token"
```

Refusal paths also verified: `--auth` on stdio, `--auth` with no secret, and a secret below
RFC 7518's 32-byte HS256 minimum all exit with a one-line message naming the fix.

**Two findings the probe produced that reading the SDK would not have** (both in ADR-007):

1. The SDK's `WWW-Authenticate` omits the RFC 6750 `scope` attribute. `ScopeChallengeMiddleware`
   adds it — the only middleware of our own, and it exists because a measurement showed the
   attribute absent.
2. **The published issuer and the accepted issuer disagreed by one character.** The RFC 9728
   document renders the issuer through pydantic's `AnyHttpUrl`, which appends a trailing
   slash to a path-less authority. RFC 8414 §2 makes issuer comparison exact, so a client
   reading `authorization_servers` from our own metadata and presenting a token with that
   exact `iss` would have been rejected by the server that published it. The verifier now
   accepts both spellings and only those two.

Also fixed while wiring: `http_app()` now resets `mcp.settings.auth` / `_token_verifier` on
every call. `mcp` is a module-level singleton, so an authenticated call would otherwise have
left auth on an app that asked for none — which Phase 4, running both in one process, would
have hit.

## Phase 4 — Tests · 60 min

HTTP-level tests via `httpx.ASGITransport` against `streamable_http_app()`, so they run in
CI with no network and no live port.

- [x] `GET /mcp` and `DELETE /mcp` → `405` with `Allow: POST`
- [x] Bad `Origin` → `403`; permitted origin → passes
- [x] `Mcp-Session-Id` sent → ignored, and **not echoed back** (the regression that would
      silently reintroduce a removed protocol feature)
- [x] Missing token → `401` with a parseable `WWW-Authenticate` naming `resource_metadata`
- [x] Token with wrong `aud` → `401` (the confused-deputy case; the single most important
      auth test here)
- [x] Expired token → `401`; valid token, missing scope → `403` `insufficient_scope`
- [x] Valid token → `tools/list` returns the same four tools
- [x] `/.well-known/oauth-protected-resource/mcp` returns a valid RFC 9728 document
- [x] **Transport parity test:** the same server over stdio and over HTTP returns byte-identical
      `tools/list` output.

### Phase 4 result

`tests/test_http_transport.py` — **31 tests, suite now 90, all green**, no network and no
live port (`httpx2.ASGITransport` against the same `http_app()` the process serves).

The file separates two kinds of test on purpose, which is the Phase 1 gate written down as
code: **guards on the SDK** (the header ladder, the 405, the Origin check — spec MUSTs the
SDK already enforces, pinned so an upgrade that drops one fails here rather than in
production) and **guards on our own code** (statelessness, the empty origin allowlist, every
auth assertion).

The parity test spawns a real `python -m regdocs_mcp` subprocess, feeds it one JSON-RPC line
over stdio, and compares the serialised tool listing against the HTTP one — with a guard
asserting four named tools first, so the comparison cannot pass on two empty listings.

**One test failed on first run and taught us something the probe had not.** Era routing
reads `MCP-Protocol-Version` *before* the classifier, so a value in the handshake set
(2024-11-05..2025-11-25) is dispatched to the legacy transport and never reaches the modern
header ladder — it cannot produce `-32020` no matter what the body envelope says. Only an
*unrecognised* version routes modern. Both halves of that asymmetry are now pinned:
`2026-01-01` mismatching the envelope → `400 -32020`; `2025-06-18` → legacy path, `200`, and
still no session ID.

## Phase 5 — Docs and ship · 45 min

- [ ] **ADR-006** — Streamable HTTP under `2026-07-28`: no sessions, no GET stream, no
      resumability, and why removing session state is the right call for a horizontally
      scaled server
- [ ] **ADR-007** — auth posture: resource server implemented, authorization server out of
      scope, audience validation is the part that actually matters
- [ ] README: HTTP quickstart, a `curl` example with the required headers, the security
      posture, and `claude mcp add --transport http regdocs http://127.0.0.1:8000/mcp
      --header "Authorization: Bearer ..."`
- [ ] Verify Claude Code connects over HTTP as well as stdio
- [ ] Commit, push, confirm CI green

## Stretch — second server · only if Phases 0–5 finish early

`market-data-mcp` over yfinance, purely to show two different tool surfaces. The prep plan
caps it at 90 minutes and its own descope list ranks it **second to cut**. Day 1's lesson was
that depth on one surface reads better than breadth: skip unless genuinely ahead.

---

## Decisions I would want a steer on before Phase 3

1. **Auth depth.** Recommendation above is a JWT verifier with real audience validation. The
   cheaper option is a static bearer token (~20 min saved) but it cannot demonstrate RFC 8707
   audience binding, which is the confused-deputy defence and the part interviewers in
   regulated shops actually probe.
2. **Whether HTTP replaces or supplements the Claude Code registration.** Recommendation:
   supplement. Keep stdio as the registered default — it needs no running process — and use
   HTTP for the demo and tests.

## Risks

1. **SDK compliance gaps.** The Phase 1 gate exists to find them before Phase 2 builds on an
   assumption. If the SDK does not validate `Mcp-Method`/`Mcp-Name` against the body, that is
   middleware we write, and it is the difference between a 90-minute and a 150-minute Phase 2.
2. **`AuthSettings` may assume a full authorization server.** The SDK's auth package includes
   `/authorize`, `/token` and registration handlers. If resource-server-only is awkward to
   configure, the fallback is bearer middleware of our own in front of the ASGI app, with the
   PRM document served as a static route.
3. **Time.** If Phase 3 overruns, ship the transport with auth **off by default** and a
   working PRM document plus 401 challenge, deferring scope handling. Never cut Phase 4's
   audience-validation test or the parity test — those are the two that carry the claim.
