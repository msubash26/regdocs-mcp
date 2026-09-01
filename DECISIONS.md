# Decisions (ADRs) — regdocs-mcp

Decisions specific to this MCP server. System-level decisions live in the
[compliance-copilot](https://github.com/msubash26/compliance-copilot) repo's `DECISIONS.md`.

---

## ADR-001 — Published as a standalone repo
**Date:** 2026-08-30 · **Status:** Accepted

**Decision.** This server ships as its own repo rather than a directory inside the copilot
monorepo, and is consumed there as an editable path dependency.

**Rationale.** It is intended to be independently useful and cloneable — the tool surface has
no dependency on the copilot. The cost is that the copilot's CI must check out both repos.

---

## ADR-002 — MCP spec revision and SDK pin
**Date:** 2026-09-01 · **Status:** Accepted

**Decision.** Target spec revision **`2026-07-28`**; pin **`mcp>=2.1,<3`** (resolved 2.1.1).

**The revision moved twice more than the prep plan assumed.** The plan tracked
2024-11-05 → 2025-03-26 → 2025-06-18 → 2025-11-25. The current revision is `2026-07-28`,
confirmed at `modelcontextprotocol.io/specification/versioning` on 2026-09-01 and echoed by
`mcp.types.LATEST_PROTOCOL_VERSION`.

**What changed at 2026-07-28 — the `initialize` handshake is gone.** Version negotiation is
now *per request*, carried in `_meta["io.modelcontextprotocol/protocolVersion"]`, and the
server accepts or rejects each request independently. A client that wants to settle the
version up front calls **`server/discover`**, a mandatory RPC returning supported versions,
capabilities and identity in one round trip. `initialize` survives only as the
backward-compatibility path for 2025-11-25 and earlier.

This matters for how the server is described: "stdio transport and the `initialize`
lifecycle" is now a *legacy-interop* topic, not a description of the current protocol.

**Why v2 of the SDK, and why the interop risk turned out to be nil.** The SDK's v2 line is
the rework that targets `2026-07-28`; v1 (latest 1.29.1) is maintenance-only. The risk in
picking a brand-new major was that real clients might not speak the new revision. Measured
before writing any tool code:

```
mcp.types.version.SUPPORTED_PROTOCOL_VERSIONS
  = ('2024-11-05', '2025-03-26', '2025-06-18', '2025-11-25', '2026-07-28')
    HANDSHAKE = the first four          MODERN = ('2026-07-28',)
```

One server, both paths, no shim of ours:

| Client path | Result |
|---|---|
| `ClientSession.initialize()` (handshake) | negotiates **2025-11-25**, tools callable |
| `ClientSession.discover()` (modern) | `supported_versions: ['2026-07-28']`, tools callable |
| `claude mcp add` → `claude mcp list` | **✔ Connected** |

So targeting the newest revision costs nothing in reach. The planned fallback to
`mcp>=1.29,<2` was not needed and is not carried.

**Also confirmed working on the v2 surface** (all four tools depend on these):
`ToolAnnotations(read_only_hint=..., idempotent_hint=..., destructive_hint=...,
open_world_hint=...)`, auto-generated `output_schema` from return-type hints, and
`structured_content` returned alongside the backward-compatible serialised-JSON text block.

**Note on naming.** SDK v2 exposes snake_case attributes on the Python models
(`server_info`, `output_schema`, `structured_content`, `is_error`) while the wire format
stays camelCase. Reading wire-format field names out of the spec and typing them into
Python is a small, repeated trap.

**Revisit if** a target client rejects `2026-07-28` outright, or when Day 2 adds Streamable
HTTP — the `MCP-Protocol-Version` header and OAuth 2.1 are that day's spec surface.

---

## ADR-003 — The index schema is the contract; the parser is replaceable
**Date:** 2026-09-01 · **Status:** Accepted

**Decision.** `regdocs_mcp.index` defines three tables — `documents`, `sections`,
`document_versions` — and the tools read only those. The Day 1 PDF parser
(`regdocs_mcp.build`) is explicitly provisional and is not imported by the server.

**The problem it solves.** Day 1 needs real content to serve, but Day 3 owns real parsing
(Docling, hierarchical chunking, contextual retrieval). Doing the parsing twice wastes a
day; waiting for Day 3 leaves Day 1 with nothing to demonstrate. Putting a schema between
them means content quality can improve without any tool signature moving. Day 3's pipeline
in `regops-ingest` writes these same tables and the tool surface does not change.

**`section_path` is a clause number, not a chunk index.** `"6.14"` is what a compliance
officer cites and what the source document actually calls that passage, so it survives
re-parsing: a different parser that finds the same clause gives it the same path. A chunk
index would change every time the chunker changed, breaking every citation ever emitted.

**Where the index lives.** Configured by `--index` or `REGDOCS_INDEX`, never bundled. The
server stays independently cloneable (ADR-001) and does not depend on the copilot's
gitignored corpus. Both mechanisms exist because hosts differ — see ADR-005.

**Measured on the Day 1 corpus** (463 MAS documents → 8,055 sections):

| doc_type | docs | median sections/doc | median chars/section |
|---|---|---|---|
| `notices` | 337 | 7 | 439 |
| `guidelines` | 126 | 18 | 825 |

Notices are short prescriptive clauses; guidelines are long advisory prose. That gap is
the "retrieval behaves differently by document type" axis Day 5 needs, and it is now
measurable rather than asserted.

**Known limits, all inherited by Day 1 only.** Tables are flattened to text. Footnotes are
mixed into clause bodies. Two documents (Notices 817 and 818) are scanned images with zero
extractable text and produce no sections at all — they need OCR, which is a Day 3 problem.

---

## ADR-004 — `diff_versions` ships with an honest empty state
**Date:** 2026-09-01 · **Status:** Accepted

**Decision.** `diff_versions` is implemented and exposed, backed by `document_versions`. A
freshly built index holds one version per document, so any real call returns a tool
execution error naming the versions on record and saying why there is nothing to diff.

**The corpus has no version pairs, and this was checked rather than assumed.** Of 463
documents, 6 are explicit amendment instruments. The single most promising candidate — two
documents both titled "Notice 501" — turned out on inspection to be neither a pair:

- `e0f6a5aa49782da7` is a **cancellation notice** (2022) that withdraws Notice 501 entirely.
- `ac134cab269c2d6f` is **MAS 501 (Amendment) 2020**, which is *itself already a diff*: MAS
  publishes amendments as a marked-up comparison ("text which is coloured and struck
  through..."), rendered visually with strikethrough.

So MAS already ships the diff, as a document, in a form a text extractor flattens into
nonsense. The planned seeding of that pair was dropped — it would have produced a demo
that looked like a diff without being one.

**Version history is created by re-fetching, not by this corpus.** ADR-012 in the copilot
makes `doc_id` a hash of the canonical URL, never of file bytes, precisely so that a
re-fetch of a reissued PDF updates the same logical document. The first re-fetch that finds
a changed `sha256` is the moment `document_versions` gets a second row. That is a Day 3
event, and there is no way to fake it earlier that is worth faking.

**Why ship the tool at all rather than cutting to three.** The empty state is a real
behaviour, not a stub: it exercises the tool-execution-error path, it tells the model what
versions exist so the call is recoverable, and the tool becomes useful the day the data
arrives with no signature change. A tool that returns a well-formed "not yet, and here is
why" is more honest than one quietly missing from the surface.

---

## ADR-005 — Tool-surface design rules
**Date:** 2026-09-01 · **Status:** Accepted

Five rules, applied to all four tools.

**1. Return stable IDs, never raw blobs.** `search_notices` returns `(doc_id,
section_path)` plus a 320-character snippet; reading the clause is a second, explicit call.
Returning matched documents whole is how a tool result blows a context window in
production.

**2. Everything is bounded.** `search_notices` and `list_obligations` paginate by opaque
cursor; `get_document_section` windows by character offset and reports `has_more` /
`next_offset`. No tool can return an unbounded result. The Day 1 demo confirmed this is not
theoretical — the model paged `list_obligations` on its own from `next_cursor`.

**3. Recoverable failures must carry the recovery path.** An unknown `section_path` returns
the valid paths for that document; an unknown `doc_id` names `search_notices` as the way to
get one; a missing version lists the versions on record.

*This has a sharp edge in the SDK.* Only the SDK's own `ToolError` reaches the model with
its message intact. Any other exception — including a plain `ValueError` or `LookupError`
raised deliberately — is treated as a crash, logged server-side, and delivered to the model
as the bare string `Error executing tool <name>`. The first implementation here used
`LookupError` and silently threw away every recovery hint it had carefully constructed.

**4. Annotate honestly.** All four are `read_only_hint=True`, `idempotent_hint=True`,
`destructive_hint=False`, `open_world_hint=False` — true, because they read a local index.
The spec warns clients to treat annotations from untrusted servers as untrusted, which is
the reason to keep them accurate rather than aspirational.

**5. Descriptions are prompt real estate, so measure them.** The four tool descriptions are
222–316 characters. Measured over the wire:

```
tools/list payload: 8,454 bytes  ~= 2,113 tokens, resident in every request
```

That is the standing cost of exposing this server, before any call is made. Worth knowing
before adding a fifth tool, and worth re-measuring when descriptions are rewritten —
the plan's suggestion to measure how tool-call accuracy moves with description wording
needs this number as its baseline.

**One divergence between the spec and its reference SDK, deliberately not papered over.**
Spec 2026-07-28 classes "unknown tool" as a *protocol* error (JSON-RPC `-32602`). The
Python SDK instead returns `isError: true` with `"Unknown tool: <name>"`. A client that
follows the spec literally and only catches protocol errors would read a misspelt tool name
as a failed call. `tests/test_protocol.py` pins down what we actually ship so a future SDK
change is visible as a test failure rather than a silent behaviour swap.

---

## ADR-006 — Streamable HTTP under `2026-07-28`: sessions are gone
**Date:** 2026-09-02 · **Status:** Accepted

**Decision.** Add Streamable HTTP alongside stdio, targeting `2026-07-28` only for new
behaviour, and run it **stateless**. stdio stays the default transport so the Day 1 Claude
Code registration keeps working untouched.

**The prep plan's Day 2 describes a transport that no longer exists.** It specifies "a
single endpoint handling POST/GET/DELETE with `Mcp-Session-Id` session management" and
resumability via `Last-Event-ID`. That was accurate for `2025-03-26`..`2025-11-25`. Revision
`2026-07-28` carries an explicit breaking-change notice:

| Prep plan (2025 revisions) | `2026-07-28` |
|---|---|
| POST / GET / DELETE on one endpoint | **POST only** — GET and DELETE are `405` with `Allow: POST` |
| `Mcp-Session-Id` session management | **Protocol-level sessions removed** — do not mint or echo |
| resumable via `Last-Event-ID` | not resumable |
| server→client requests on the SSE stream | replaced by MRTR (`InputRequiredResult` → client retries with `inputResponses`) |
| standalone GET stream for notifications | long-lived notifications come from a `subscriptions/listen` response stream |
| — | `Mcp-Method` required on every request; `Mcp-Name` on `tools/call` / `prompts/get` / `resources/read` |
| — | headers **MUST** be validated against the body → `400` + `-32020 HeaderMismatch` |
| — | `Origin` **MUST** be validated (DNS-rebinding defence) |

**Why the spec removed sessions, which is the part worth being able to say.** A session ID
is server-side affinity. It forces either sticky routing or shared session state, so a
horizontally scaled deployment pays for a concept the protocol never needed: every field a
session carried (protocol version, client capabilities, client info) is per-request data,
and `2026-07-28` moved it into the request envelope under
`params._meta["io.modelcontextprotocol/protocolVersion"]` and
`.../clientCapabilities`. A self-contained POST load-balances to any replica, retries
safely, and needs no eviction policy. The `initialize` handshake went for the same reason —
it existed to establish the state that no longer exists. Deleting the handshake and deleting
sessions are one change, not two.

**The SDK enforces every transport MUST — verified by probe, not by reading.** `mcp` 2.1.1
serving `MCPServer.streamable_http_app()`, driven through `httpx2.ASGITransport`:

```
GET /mcp                                    -> 405, Allow: POST
DELETE /mcp                                 -> 405, Allow: POST
Mcp-Method disagrees with body.method       -> 400, -32020
Mcp-Method absent                           -> 400, -32020
Mcp-Name disagrees with params.name         -> 400, -32020
Mcp-Name absent on tools/call               -> 400, -32020
MCP-Protocol-Version header != envelope     -> 400, -32020
Mcp-Method sent twice (duplicate header)    -> 400, -32020
params._meta envelope absent                -> 400, -32602
unknown method                              -> 404, -32601
Mcp-Session-Id sent on a modern request     -> 200, ignored, not echoed
Origin: http://evil.example                 -> 403
Host: evil.example                          -> 421
```

So the Phase 1 gate found **nothing to reimplement**. The header↔body ladder lives in
`mcp/shared/inbound.py::classify_inbound_request`, the status mapping in
`ERROR_CODE_HTTP_STATUS`, and `streamable_http_app(host="127.0.0.1")` auto-enables DNS
rebinding protection with a localhost host/origin allowlist. These get *tested*, not
duplicated — the tests are a regression guard on an SDK we do not control.

**One gap, and it is a default rather than a missing feature.** Era routing is by header
alone (`streamable_http_manager.py`): a POST whose `MCP-Protocol-Version` names a handshake
revision — **or omits the header entirely** — is routed to the legacy stateful transport,
which mints and echoes a session ID:

```
stateless_http=False  legacy initialize, no version header -> 200, Mcp-Session-Id=e3226cef…
stateless_http=True   legacy initialize, no version header -> 200, Mcp-Session-Id=None
stateless_http=True   modern tools/list                    -> 200, ok
```

An unconfigured server therefore reintroduces, on its back-compat path, the exact protocol
feature `2026-07-28` deleted. **`stateless_http=True` closes it** with no middleware, and
without dropping backward compatibility: legacy clients are still served, they just get no
session. That is the honest posture — reach at no cost, which is the same finding ADR-002
recorded for protocol versions — and it is why the "`Mcp-Session-Id` is never echoed" test
is written against a header-less request, not only a modern one. A test that only probes
the modern path would pass on a server that mints sessions all day.

**Rejected: rejecting non-modern revisions over HTTP.** It would make "no sessions" true by
construction rather than by configuration, but it discards clients the SDK serves correctly
for free, and it makes this server stricter than the spec, which keeps back-compat
deliberate. Statelessness gets the same guarantee at lower cost.

---

## ADR-007 — Auth posture: the resource-server half, done properly
**Date:** 2026-09-02 · **Status:** Accepted

**Decision.** Implement this server as an OAuth 2.1 **protected resource** and nothing else.
Bearer tokens are signed JWTs, validated for signature, expiry, issuer, **audience** and
scope. Authorization is **off by default**, opt-in with `--auth`, and rejected outright on
stdio. No authorization server is implemented.

**The prep plan's bar is below the spec's.** It says "a token check plus a written note on
the OAuth flow is enough for a portfolio piece." Under `2026-07-28` authorization is optional
overall, but a server that offers it MUST publish RFC 9728 metadata, challenge with `401` +
`WWW-Authenticate`, validate the token audience per RFC 8707, and answer an under-scoped
token with `403 insufficient_scope`. `if token == SECRET` is none of those, and the gap is
not cosmetic — it is the whole of the security argument.

**Audience validation is the part that matters, and the SDK does not do it.** The SDK
verifies the `Bearer` scheme, calls our verifier, and enforces scopes. It never inspects
`aud`. A resource server that accepts any correctly-signed token from its issuer — including
one minted for a *different* resource — is a confused deputy: a token the user consented to
give service A silently becomes a credential for service B. `jwt.decode(audience=...)` in
`auth.py` is that check, the audience is this server's canonical URI
(`http://host:port/mcp`), and it is the single most important line in the module.

**Why a JWT verifier rather than a static bearer token.** A string compare is ~20 minutes
cheaper and cannot demonstrate any of it: no audience binding, no expiry, no scope, no
issuer. The JWT verifier is barely more code and makes each property real and testable.
`mint_token()` stands in for the AS so the tests can construct the wrong-audience, expired,
under-scoped and bad-signature cases as *tokens* rather than as mocks.

**Measured on a live server** (`--transport http --port 8079 --auth`):

```
GET /.well-known/oauth-protected-resource/mcp
  -> 200 {"resource":"http://127.0.0.1:8000/mcp",
          "authorization_servers":["https://auth.invalid/"],
          "scopes_supported":["regdocs:read"],
          "bearer_methods_supported":["header"]}

no token       -> 401 error="invalid_token"      + resource_metadata + scope
valid          -> 200 tools/list
wrong audience -> 401 error="invalid_token"      <- the confused-deputy case
missing scope  -> 403 error="insufficient_scope"
expired        -> 401 error="invalid_token"
bad signature  -> 401 error="invalid_token"
```

Every rejection is deliberately indistinguishable to the caller: the SDK's `TokenVerifier`
protocol has exactly one failure channel (`None`), and a verifier that explained *which*
check failed would help an attacker enumerate.

**Two things the probe found that reading would not have.**

*1. The SDK's challenge omits the RFC 6750 `scope` attribute.* It emits `error`,
`error_description` and `resource_metadata`. RFC 6750 §3.1 says a `403 insufficient_scope`
SHOULD name the scope required, and a client that has to parse `error_description` prose to
learn it is a client that will get it wrong. `ScopeChallengeMiddleware` appends it. This is
the only middleware of our own in the stack, and it exists because a measurement showed the
attribute absent — not because it was assumed missing.

*2. The published issuer and the accepted issuer disagreed by one character.* The RFC 9728
document renders the issuer through pydantic's `AnyHttpUrl`, which appends a trailing slash
to a path-less authority: configured `https://auth.invalid`, published
`https://auth.invalid/`. RFC 8414 §2 makes issuer comparison *exact string comparison*, so
a client that read `authorization_servers` from our own metadata and presented a token whose
`iss` matched it verbatim would have been rejected by the server that published it. The
verifier now accepts both spellings and only those two — one character wide, no prefix
matching, no case folding. Comparison stays exact; it is the set that has two members.

**Deliberately not implemented, and what a real AS changes.** No `/authorize`, no `/token`,
no dynamic client registration, no PKCE, no refresh, no consent screen, no revocation. Those
belong to an authorization server — Keycloak, Auth0, Entra — and standing one up would
demonstrate nothing this server is responsible for. Putting a real one in front changes
three things and no more: `RS256` over a JWKS fetched from the issuer's metadata instead of
a shared secret, `issuer_url` pointed at the AS, and the dev minter deleted. The verifier's
shape — signature, expiry, issuer, audience, scope — is unchanged, because that shape is
what a resource server owes regardless of who issues the token.

**The challenge chain works, and a real client proved it in a way curl could not.**
Registering the server in Claude Code *without* a token produced not a bare failure but this:

```
regdocs-http-noauth: ✘ Failed to connect — Dynamic Client Registration rejected
  (HTTP 400): Port 9000 is for clickhouse-client program
```

That is the whole RFC 9728 flow executing correctly: `401` → read `WWW-Authenticate` → fetch
the protected-resource metadata → find `authorization_servers` → attempt registration
against it. It failed only because the placeholder issuer was `http://127.0.0.1:9000`, and
on this box port 9000 is the LangFuse stack's ClickHouse. A loopback placeholder is worse
than useless for an issuer, because a conforming client *will* dial it. The default is now
`https://auth.invalid` — RFC 2606 reserves `.invalid`, so it can never resolve, and an
operator who enables `--auth` without setting `$REGDOCS_AUTH_ISSUER` gets an unambiguous DNS
failure instead of an answer from whatever happens to be listening locally.

**stdio never authenticates.** `--auth` with `--transport stdio` exits with an error rather
than being ignored. A stdio server is a subprocess of its client and inherits that client's
trust; the spec says stdio implementations SHOULD NOT use this flow and should take
credentials from the environment. Silently accepting a flag that does nothing would be worse
than refusing it.

**HS256 keys are length-checked at construction.** RFC 7518 §3.2 requires an HMAC key at
least as long as the hash output (32 bytes for SHA-256). PyJWT only warns, and a warning on
stderr is not a control, so a short secret is refused with a message naming the fix.
