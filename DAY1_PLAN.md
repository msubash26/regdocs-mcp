# Day 1 — MCP server v1

**Repo:** `regdocs-mcp` · **Target:** spec `2026-07-28`, SDK `mcp>=2.1,<3`
**Budget:** ~4.5h build + 1h write-up · **Date:** 2026-09-01 · **Status: complete**

---

## Context

Day 0 is complete (all 7 stack containers healthy, 463-document MAS corpus fetched and
manifested, both repos pushed, ADR-001..012 written). `regdocs-mcp` is still a bare
scaffold: one `hello()` function, one placeholder test, `mcp` not yet a dependency.

### Research completed before planning (ADR-002 requires this)

- **Current spec revision: `2026-07-28`** — two revisions past the newest one the prep plan
  mentions (2025-11-25).
- **SDK: `mcp` 2.1.1** (v2 line, a rework targeting 2026-07-28). The v1 line is at 1.29.1
  and is maintenance-only.
- **The material change:** 2026-07-28 **removed the `initialize` handshake**. Version
  negotiation is now per-request via `_meta["io.modelcontextprotocol/protocolVersion"]`,
  plus a mandatory `server/discover` RPC. "Cover the `initialize` lifecycle" is now a
  *backward-compatibility* topic, not a core one.
- v2 API shape: `from mcp.server import MCPServer` → `@mcp.tool()` →
  `mcp.run(transport="stdio")`. HTTP client is `httpx2`.
- Results carry `resultType`; `tools/list` supports `cursor`/`nextCursor` + `ttlMs`/
  `cacheScope`; `structuredContent` + `outputSchema`; protocol errors (JSON-RPC `-32602`)
  are distinct from tool-execution errors (`isError: true`).

### Two problems Day 1 has to solve

**1. There is no text.** The corpus is 463 raw PDFs. Day 3 owns real parsing (Docling,
hierarchical chunking, contextual retrieval). Day 1 needs working tools without doing Day
3's work twice, and without making the public `regdocs-mcp` repo depend on a gitignored
corpus in the other repo.

*Resolution:* `regdocs-mcp` owns a **schema contract**, not a parser. It reads a DuckDB
index located by `REGDOCS_INDEX`. Day 1 ships a provisional builder; Day 3 replaces the
builder with the Docling pipeline writing the same schema. The tool surface never changes.

**2. `diff_versions` has almost no data.** Only 6 explicit amendment PDFs out of 463, and
exactly **one** genuine base+amendment pair (Notice 501). The 9 repeated notice codes are
notice-vs-guidelines pairs, not versions. Per ADR-012's URL-stable `doc_id`, version
history is genuinely *born* on Day 3's first idempotent re-fetch.

*Resolution:* ship the tool with an honest empty state, seed the one real pair.

---

## Phase 0 — Housekeeping · 20 min

- [x] `gh auth switch --user msubash26` (active account has flipped back to `99Tungsten99`;
      any `gh` command here would hit the wrong account)
- [x] Rewrite `initial-setup.md` as an accurate status doc — Day 0 complete, 7 containers
      up, 463-doc corpus, both repos pushed, ADR-001..012. Keep the gotchas section.

## Phase 1 — Pin the spec, de-risk the client · 30 min

- [x] `uv add "mcp[cli]>=2.1,<3"`; read the installed SDK source for the real names of the
      annotation, pagination and `outputSchema` APIs (docs only show decorator basics)
- [x] **Gate before any tool code:** a two-line `MCPServer` with one trivial tool, run under
      MCP Inspector *and* `claude mcp add`. Confirm Claude Code negotiates `2026-07-28` (or
      that the SDK's compat path handles it). If not, fall back to `mcp>=1.29,<2` here —
      cost is ~30 min, not the day.
- [x] Draft ADR-002

## Phase 2 — The index contract · 90 min

`regdocs-mcp` defines the schema and reads it from `REGDOCS_INDEX` (DuckDB file). It does
not own the parser.

```sql
documents(doc_id PK, issuer, doc_type, title, url, source_page,
          sha256, fetched_at, n_sections)
sections(doc_id, section_path, heading, ordinal, text,
         char_len, page_from, page_to)          -- PK (doc_id, section_path)
document_versions(doc_id, version_label, sha256, fetched_at, filename)
```

- [x] `regdocs-index build` CLI: PyMuPDF page text → heading-based section split → DuckDB
      FTS (BM25). Reads `corpus/manifest.jsonl` for metadata so `doc_id` matches ADR-012's
      URL-derived ID exactly — Day 3 re-ingestion lands on the same keys.
- [x] **Validate on 20 docs before the full 463.** Guard: if median sections-per-doc is 1,
      the heading heuristic failed and needs a second pass.
- [x] `document_versions` gets one row per doc, plus the hand-seeded Notice 501 pair.
- [x] Docstring stating the builder is provisional and Day 3's Docling pipeline replaces it
      behind this schema.

## Phase 3 — The four tools · 120 min

| Tool | Signature | Notes |
|---|---|---|
| `search_notices` | `(query, issuer?, doc_type?, date_from?, top_k=10, cursor?)` | BM25 ranked; returns `doc_id` + `section_path` + score + snippet. Never full text. |
| `get_document_section` | `(doc_id, section_path, offset=0, max_chars=4000)` | Returns `has_more` + `next_offset`. The "stable IDs, not 40KB blobs" point. |
| `list_obligations` | `(doc_id, cursor?)` | Deterministic clause extraction — modal verbs (`shall`/`must`/`may not`) + clause path. Rule-based *is* the measurable baseline. |
| `diff_versions` | `(doc_id, v1, v2)` | Real changed clauses on the 501 pair. Unknown pair → `isError: true`, "only one version on record; history begins at first re-fetch." |

Cross-cutting, all four:

- [x] `outputSchema` + `structuredContent` on every tool (pulled forward from Day 2 — nearly
      free with v2's type hints)
- [x] Annotations: `readOnlyHint: true`, `idempotentHint: true`, `openWorldHint: false`
- [x] Every unbounded return paginated. No tool can blow a context window.
- [x] Descriptions written deliberately, then **measure the `tools/list` token cost** and
      record it. The plan says budget them; a number makes that real.

## Phase 4 — Tests against the JSON-RPC surface · 45 min

Delete `test_scaffold.py`. In-memory client session, testing the protocol not just the
functions:

- [x] `tools/list` returns 4 tools, deterministic order, valid `inputSchema`/`outputSchema`
- [x] each tool's happy path against a small fixture index
- [x] unknown tool → JSON-RPC `-32602`; unknown `doc_id` → `isError: true`
- [x] cursor round-trip returns disjoint pages and terminates
- [x] `structuredContent` validates against the declared `outputSchema`

## Phase 5 — Inspector + Claude Code · 45 min

- [x] `npx @modelcontextprotocol/inspector uv run regdocs-mcp` — exercise all four by hand
- [x] `claude mcp add regdocs -- uv run --directory /home/subash/regops/regdocs-mcp regdocs-mcp`
- [x] **Done when:** *"What are a bank's customer due diligence obligations under MAS Notice
      626, and what guidance expands on them?"* drives `search_notices` →
      `get_document_section` → `list_obligations` in sequence. Capture the transcript.

Note: Claude Desktop is Mac/Windows only, so on this Ubuntu box the "wire it into a real
host" requirement is satisfied via Claude Code.

## Phase 6 — Write-up · 45 min

- [x] **ADR-002** spec revision + SDK pin, and why `initialize` is now a compat topic
- [x] **ADR-003** the index schema is the contract; the Day-1 parser is provisional
- [x] **ADR-004** `diff_versions` ships with an honest empty state
- [x] **ADR-005** tool-surface design — stable IDs, mandatory pagination, structured output,
      read-only annotations, measured description budget
- [x] README: 30-second quickstart, real tool table, spec revision + SDK pin stated
      prominently, architecture sketch
- [x] Commit, push, confirm CI green

---

## Deliverables

Four working tools over 463 real MAS documents · JSON-RPC-level test suite · 4 ADRs ·
demo transcript · green CI on a public repo.

## Risks

1. **SDK v2 / Claude Code interop** — Phase 1 gate catches it before it costs more than 30 min.
2. **Heading detection on hostile PDFs** — MAS notices are clause-numbered and should split
   well; guidelines are prose and will be worse. 20-doc validation gate, and `doc_type` in
   the schema means Day 5 can measure exactly that difference.
3. **Time** — if Phase 3 overruns, `diff_versions` degrades to empty-state-only (drop the
   501 seeding) rather than cutting tests or the write-up.


---

## Outcome

All six phases complete. 59 tests green, ruff clean, four tools live in Claude Code and
MCP Inspector.

**Where the plan was wrong, and what replaced it:**

- **The interop risk was nil, not moderate.** The SDK's v2 server answers all five spec
  revisions (2024-11-05 through 2026-07-28), so targeting the newest cost nothing in
  reach. The planned `mcp>=1.29,<2` fallback was never needed. Measured in Phase 1 before
  any tool code, which is the only reason it was cheap to find out.
- **`diff_versions` lost its seed data on inspection.** The plan assumed one genuine
  base+amendment pair (Notice 501). Neither document is a version: one is a cancellation
  notice, the other is an amendment instrument that is *itself* a rendered diff. Shipped
  with the honest empty state only — the plan's documented fallback, reached for a better
  reason than running out of time. See ADR-004.
- **The splitter needed two corrections the plan did not anticipate.** Footnote markers
  parsed as clause numbers (fixed with a monotonic-succession guard), and page numbers ate
  real section markers (fixed by disambiguating on position within the page). The 20-doc
  validation gate caught the first; the second only showed up on a specific document.
  Both now have regression tests.
- **`date_from` would have been decorative.** Nothing in the schema could satisfy it, so
  `effective_date` was added to the contract and extracted from front matter — resolved for
  341 of 463 documents (74%). Documents with no stated date are excluded from a filtered
  search rather than assumed recent.
- **Two scope additions, both small and both forced by evidence:** a `--index` flag
  (MCP Inspector sanitises the environment, so an env-only server is unreachable there),
  and an obligation-extractor fix for enumerated limbs — a bug the Day 1 demo surfaced on
  its own.

**Findings worth carrying forward:**

- Only the SDK's `ToolError` reaches the model with its message; every other exception is
  treated as a crash and stripped to `Error executing tool <name>`.
- The spec calls "unknown tool" a protocol error; the reference SDK returns `isError: true`.
  Pinned down in a test rather than papered over.
- `tools/list` costs 2,113 tokens resident. Baseline for the description-budget experiment.
- Notices average 439 chars/section against guidelines' 825 — the doc-type axis Day 5 needs
  is real and measurable.
- Notices 817 and 818 are scanned images with zero extractable text. They need OCR (Day 3).
