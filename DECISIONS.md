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
