# regdocs-mcp

An MCP server exposing Singapore MAS regulatory documents — notices and guidelines — as
searchable, citable tools.

**Spec revision:** `2026-07-28` · **SDK:** `mcp>=2.1,<3` · **Transport:** stdio
(Streamable HTTP on Day 2) · **Status:** Day 1 — four tools, tested, wired into Claude Code.

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

## Tools

| Tool | Signature | Returns |
|---|---|---|
| `search_notices` | `(query, issuer?, doc_type?, date_from?, top_k=10, cursor?)` | Ranked clauses as `(doc_id, section_path)` + snippet. Paginated. |
| `get_document_section` | `(doc_id, section_path, offset=0, max_chars=4000)` | One clause's text, windowed, with `has_more` / `next_offset`. |
| `list_obligations` | `(doc_id, cursor?)` | Obligations tagged `requirement` / `prohibition` / `permission`. Paginated. |
| `diff_versions` | `(doc_id, v1, v2)` | Changed clauses between two versions. See ADR-004 — a fresh index has one version per document, so this reports what versions exist. |

All four are read-only and annotated as such. Every one is bounded: nothing here can return
an unbounded result into a context window.

`section_path` is the document's own clause number (`"6.14"`), not a chunk index — it is
what a compliance officer cites, and it survives re-parsing.

## Architecture

```
   MCP host (Claude Code, Inspector, LangGraph agent)
        │  stdio, JSON-RPC, spec 2026-07-28
        ▼
   regdocs_mcp.server ─── four tools, structured outputs, cursors
        │
        ▼
   regdocs_mcp.index ──── THE CONTRACT: documents · sections · document_versions
        ▲                 (DuckDB + FTS/BM25, located by --index or $REGDOCS_INDEX)
        │
   ┌────┴──────────────────────────┐
   │ regdocs_mcp.build             │  Day 1, provisional: PyMuPDF + clause splitting
   │ regops-ingest (Day 3)         │  replaces it — Docling, tables, contextual chunks
   └───────────────────────────────┘
```

The server never imports the parser. The schema is the contract, so content quality can
improve without a tool signature moving (ADR-003).

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
uv run pytest -q        # 59 tests
uv run ruff check .
uv run ruff format .
```

Tests build their own synthetic index, so the suite never depends on the fetched corpus and
runs clean in CI. They exercise the JSON-RPC surface — schemas, annotations, cursor
round-trips, `isError` vs protocol errors — not just the Python functions.

## See also

- [`DECISIONS.md`](DECISIONS.md) — ADRs, including why `2026-07-28` and what changed in it
- [`docs/DAY1_DEMO.md`](docs/DAY1_DEMO.md) — a real compliance question chaining three tools
