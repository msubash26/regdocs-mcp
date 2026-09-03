# regdocs-mcp

An MCP server exposing Singapore MAS regulatory documents — notices and guidelines — as
searchable, citable tools.

**Spec revision:** `2026-07-28` · **SDK:** `mcp>=2.1,<3` · **Transports:** stdio +
Streamable HTTP · **Status:** Day 2 — four tools, two transports, audience-validated bearer
auth, 96 tests. Ranking made reproducible on Day 6 (ADR-008); parallel tool calls made
safe on Day 7 (ADR-009).

Built for a regulatory compliance copilot, but useful on its own: point it at an index and
any MCP host can search Singapore financial regulation and cite it by clause number.

---

## Quickstart (30 seconds)

```bash
uv sync

# Build an index from a fetched corpus (a directory holding manifest.jsonl + PDFs)
uv run regdocs-index build --corpus ../compliance-copilot/corpus --out regdocs.duckdb

# Run the server
uv run regdocs-mcp --index regdocs.duckdb
```

Wire it into Claude Code:

```bash
claude mcp add regdocs --env REGDOCS_INDEX=$PWD/regdocs.duckdb \
  -- uv run --directory $PWD regdocs-mcp
```

Or poke at it with MCP Inspector. Note the `env` wrapper — Inspector spawns stdio servers
with a sanitised environment, so the index has to be passed in explicitly:

```bash
npx @modelcontextprotocol/inspector --cli \
  env REGDOCS_INDEX=$PWD/regdocs.duckdb uv run regdocs-mcp --method tools/list
```

## Over HTTP

stdio is the default because it needs no running process. The same server, same four tools,
over Streamable HTTP:

```bash
uv run regdocs-mcp --transport http --port 8000 --index regdocs.duckdb
```

```bash
claude mcp add --transport http regdocs http://127.0.0.1:8000/mcp
```

Spec `2026-07-28` requires two headers on every request — `MCP-Protocol-Version` and
`Mcp-Method`, plus `Mcp-Name` on `tools/call` — and they are validated against the body.
A hand-rolled call therefore looks like this:

```bash
curl -sS http://127.0.0.1:8000/mcp \
  -H 'content-type: application/json' \
  -H 'accept: application/json, text/event-stream' \
  -H 'mcp-protocol-version: 2026-07-28' \
  -H 'mcp-method: tools/list' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{"_meta":{
        "io.modelcontextprotocol/protocolVersion":"2026-07-28",
        "io.modelcontextprotocol/clientCapabilities":{}}}}'
```

Get `mcp-method` wrong and you get `400` with JSON-RPC `-32020 HeaderMismatch`, not a
confusing success.

### With auth

Authorization is off unless you ask for it. Turned on, this server is an OAuth 2.1
**protected resource**: RFC 9728 metadata, a `401` challenge that names it, and bearer
tokens validated for signature, expiry, issuer, **audience** and scope.

```bash
export REGDOCS_JWT_SECRET=$(uv run python -c 'import secrets; print(secrets.token_urlsafe(32))')
uv run regdocs-mcp --transport http --auth --index regdocs.duckdb

# in another shell — stands in for an authorization server, dev only
TOKEN=$(REGDOCS_JWT_SECRET=$REGDOCS_JWT_SECRET uv run python -m regdocs_mcp.auth)
claude mcp add --transport http regdocs http://127.0.0.1:8000/mcp \
  --header "Authorization: Bearer $TOKEN"
```

```bash
curl -sS http://127.0.0.1:8000/.well-known/oauth-protected-resource/mcp
# {"resource":"http://127.0.0.1:8000/mcp","authorization_servers":["https://auth.invalid/"],
#  "scopes_supported":["regdocs:read"],"bearer_methods_supported":["header"]}
```

## Security posture

| Control | Behaviour |
|---|---|
| Bind address | `127.0.0.1` by default; a non-loopback `--host` warns on stderr |
| `Origin` | Validated; allowlist starts **empty**, widen with `--allow-origin` |
| `Host` | Loopback names only, any port → `421` otherwise |
| Method | `POST` only; `GET`/`DELETE` → `405 Allow: POST` |
| Sessions | None. `Mcp-Session-Id` is never minted or echoed, on either routing path |
| Body size | 4 MiB cap (SDK default) |
| Auth | Off by default. `--auth` requires an audience-validated JWT with `regdocs:read` |
| stdio | Never authenticates — `--auth` on stdio is an error, not a no-op |

The origin allowlist is deliberately stricter than the SDK's, which seeds it with
`http://localhost:*` — that would let any page served from any local port drive the server
from a browser.

**What is not implemented:** no authorization server. No `/authorize`, `/token`, dynamic
client registration, PKCE or refresh. This is the resource-server half, and audience
validation (RFC 8707) is the part that carries the security argument — a server that accepts
any correctly-signed token from its issuer, including one minted for a *different* resource,
is a confused deputy. Putting a real AS (Keycloak, Auth0, Entra) in front changes three
things: `RS256` over a JWKS, `issuer_url` repointed, and the dev token minter deleted. See
[ADR-007](DECISIONS.md).

The issuer defaults to `https://auth.invalid` (RFC 2606 — it can never resolve) precisely
because a conforming client *does* dial it: a client sent an unauthenticated request, read
the `401` challenge, fetched the metadata above, and tried Dynamic Client Registration
against the issuer. Set `$REGDOCS_AUTH_ISSUER` to a real authorization server to make that
flow complete.

## Tools

| Tool | Signature | Returns |
|---|---|---|
| `search_notices` | `(query, issuer?, doc_type?, date_from?, top_k=10, cursor?)` | Ranked clauses as `(doc_id, section_path)` + snippet. Paginated. |
| `get_document_section` | `(doc_id, section_path, offset=0, max_chars=4000)` | One clause's text, windowed, with `has_more` / `next_offset`. |
| `list_obligations` | `(doc_id, cursor?)` | Obligations tagged `requirement` / `prohibition` / `permission`. Paginated. |
| `diff_versions` | `(doc_id, v1, v2)` | Changed clauses between two versions. See ADR-004 — a fresh index has one version per document, so this reports what versions exist. |

All four are read-only and annotated as such. Every one is bounded: nothing here can return
an unbounded result into a context window.

`search_notices` is **reproducible**: the same query and arguments return the same ranking.
That sounds like it needs no saying, and it was untrue here until Day 6. The order clause has
carried three deterministic tie-break columns since Day 1, but they fire only on exact equality
and DuckDB computes BM25 in a parallel reduction — so two clauses whose true scores are equal
came back differing in the last bit, the tie-breaks never ran, and **9 of 40 real questions
returned a different top-20 between runs**. Ordering on a rounded score makes the near-tie a
real tie: 9 of 40 → **0 of 40**. A tool whose output is not a function of its input cannot be
cached and cannot be diffed between agent runs. See [ADR-008](DECISIONS.md).

The four tools are also safe to call **in parallel on one session**, which they were not until
Day 7. Every handler shared a single DuckDB connection, on the reasoning that a read-only
connection is safe to share; read-only protects the file, not the connection's one statement
context. Four concurrent `list_obligations` calls reported a valid `doc_id` as **missing** on
2 of 4, 1 of 4 and 1 of 4 calls across three trials, and 0 of 4 sequentially. That failure is
worse than a wrong ranking because it inverts silently — the caller is told authoritatively that
a document is not in the corpus, one call after the same server returned its id from a search.
Each handler now gets its own cursor. See [ADR-009](DECISIONS.md).

`section_path` is the document's own clause number (`"6.14"`), not a chunk index — it is
what a compliance officer cites, and it survives re-parsing.

## Architecture

```
   MCP host (Claude Code, Inspector, LangGraph agent)
        │
        ├── stdio ──────────────┐   JSON-RPC, spec 2026-07-28
        │                       │
        └── Streamable HTTP ────┤   POST only · stateless · Origin-checked
             (+ optional JWT)   │   401/403 per RFC 9728 + RFC 8707
                                ▼
   regdocs_mcp.server ─── four tools, structured outputs, cursors
        │
        ▼
   regdocs_mcp.index ──── THE CONTRACT: documents · sections · document_versions
        ▲                 (DuckDB + FTS/BM25, located by --index or $REGDOCS_INDEX)
        │
   ┌────┴──────────────────────────┐
   │ regdocs_mcp.build             │  provisional: PyMuPDF + clause splitting
   │ regops-ingest (Day 3)         │  supersedes it — Docling, tables, contextual chunks
   └───────────────────────────────┘
```

The server never imports the parser. The schema is the contract, so content quality can
improve without a tool signature moving (ADR-003) — and on Day 3 it did: `regops-ingest`
recovers 11,171 clauses to this builder's 8,055, plus 2,173 tables, with **no edit to any
tool**. The copilot's `ingest/tests/test_contract.py` drives these four tools over JSON-RPC
against an index that pipeline built, so the claim is a test rather than a sentence.

`build.py` is retained deliberately. Docling needs 121 packages including torch and 15 CUDA
wheels; keeping a light builder here is what lets this repo stay cloneable and CI-green on
its own.

## Index scale

Built over 463 MAS documents (337 notices, 126 guidelines) → 8,055 sections in ~80s.

| doc_type | docs | median sections/doc | median chars/section |
|---|---|---|---|
| `notices` | 337 | 7 | 439 |
| `guidelines` | 126 | 18 | 825 |

Notices are short prescriptive clauses; guidelines are long advisory prose.

## Cost of exposing this server

```
tools/list payload: 8,454 bytes ~= 2,113 tokens, resident in every request
```

Tool descriptions are prompt real estate. That is the standing cost before any call is
made — measured, not guessed (ADR-005).

## Development

```bash
uv run pytest -q        # 96 tests
uv run ruff check .
uv run ruff format .
```

Tests build their own synthetic index, so the suite never depends on the fetched corpus and
runs clean in CI. They exercise the JSON-RPC surface — schemas, annotations, cursor
round-trips, `isError` vs protocol errors — not just the Python functions.

The HTTP suite runs over `httpx2.ASGITransport`, so it needs no network and no live port. It
separates **guards on the SDK** (spec MUSTs `mcp` already enforces, pinned so an upgrade that
drops one fails here rather than in production) from **guards on our own code**
(statelessness, the origin allowlist, every auth assertion). A parity test spawns a real
stdio subprocess and asserts its `tools/list` is byte-identical to the HTTP one — the claim
"the same server runs both transports" is a test, not a sentence.

## See also

- [`DECISIONS.md`](DECISIONS.md) — ADRs: why `2026-07-28`, why sessions were removed
  (ADR-006), and the auth posture (ADR-007)
- [`docs/DAY1_DEMO.md`](docs/DAY1_DEMO.md) — a real compliance question chaining three tools
