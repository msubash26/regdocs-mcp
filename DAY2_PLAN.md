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

- [ ] `gh auth status` — it drifts back to `99Tungsten99`; switch to `msubash26` if needed
- [ ] `./scripts/stack.sh ps` in the copilot — 7 services, only needed if tracing HTTP calls
- [ ] Re-read Day 1's "Outcome" section in `DAY1_PLAN.md`

## Phase 1 — Pin the transport delta · 30 min

Research is already done (table above). Remaining:

- [ ] Confirm against the installed SDK which of the `2026-07-28` transport MUSTs it
      actually enforces — specifically `Mcp-Method` / `Mcp-Name` presence and the
      header↔body validation producing `-32020`. **This is the gate:** anything the SDK does
      not enforce, we add as middleware, and anything it does enforce we test rather than
      duplicate.
- [ ] Draft **ADR-006** — the transport delta and why sessions were removed

## Phase 2 — Streamable HTTP transport · 90 min

- [ ] `regdocs-mcp --transport {stdio,http}`, with `--host`, `--port`, `--path` (default
      `/mcp`). stdio stays the default so Day 1's Claude Code registration keeps working
      untouched.
- [ ] `TransportSecuritySettings` wired with a sane local default (`127.0.0.1` host allowlist,
      empty origin allowlist) and a `--allow-origin` flag for deliberate widening
- [ ] Any header validation the Phase 1 gate showed missing, added as ASGI middleware
- [ ] Manual verification against a live server: `POST /mcp` works; `GET /mcp` and
      `DELETE /mcp` return `405` with `Allow: POST`; a bogus `Origin` returns `403`; an
      `Mcp-Session-Id` header is ignored and not echoed; a mismatched `Mcp-Name` returns
      `-32020`

## Phase 3 — Authorization, resource-server half · 90 min

- [ ] A `TokenVerifier` implementation. **Recommendation: a signed-JWT verifier** that checks
      signature, expiry, `aud` against this server's canonical URI, and `scope` — with the
      signing key from the environment. It is barely more work than a string compare and it
      makes audience validation real rather than gestured at.
- [ ] `AuthSettings(issuer_url=..., resource_server_url=..., required_scopes=["regdocs:read"])`
- [ ] Confirm the SDK serves `/.well-known/oauth-protected-resource/mcp` and that the
      document names the issuer and `scopes_supported`
- [ ] `401` carries `WWW-Authenticate: Bearer resource_metadata="...", scope="regdocs:read"`
- [ ] `403` + `error="insufficient_scope"` when the token is valid but under-scoped
- [ ] Auth is **off by default and opt-in via `--auth`**. stdio must never require it — the
      spec says stdio implementations SHOULD NOT use this flow and should take credentials
      from the environment.
- [ ] Write down what is deliberately *not* implemented: no authorization server, no
      `/authorize`, no `/token`, no dynamic client registration, no PKCE — and what would
      change if a real AS (Keycloak, Auth0) were put in front.

## Phase 4 — Tests · 60 min

HTTP-level tests via `httpx.ASGITransport` against `streamable_http_app()`, so they run in
CI with no network and no live port.

- [ ] `GET /mcp` and `DELETE /mcp` → `405` with `Allow: POST`
- [ ] Bad `Origin` → `403`; permitted origin → passes
- [ ] `Mcp-Session-Id` sent → ignored, and **not echoed back** (the regression that would
      silently reintroduce a removed protocol feature)
- [ ] Missing token → `401` with a parseable `WWW-Authenticate` naming `resource_metadata`
- [ ] Token with wrong `aud` → `401` (the confused-deputy case; the single most important
      auth test here)
- [ ] Expired token → `401`; valid token, missing scope → `403` `insufficient_scope`
- [ ] Valid token → `tools/list` returns the same four tools
- [ ] `/.well-known/oauth-protected-resource/mcp` returns a valid RFC 9728 document
- [ ] **Transport parity test:** the same server over stdio and over HTTP returns byte-identical
      `tools/list` output. The Day 2 claim is "the same server runs both transports" — that
      should be asserted by a test, not by a sentence in the README.

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
