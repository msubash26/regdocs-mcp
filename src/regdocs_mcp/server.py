"""The regdocs-mcp server: four tools over Singapore MAS regulatory documents.

Targets MCP spec revision 2026-07-28 (DECISIONS.md ADR-002). The SDK also answers
the four handshake-based revisions back to 2024-11-05, so older clients work
without a compatibility shim of our own.

Design rules applied to every tool here (ADR-005):
  - Return stable IDs, never raw blobs. A hit gives you (doc_id, section_path);
    you fetch the text with a second call if you want it.
  - Everything is bounded. No tool can return an unbounded result.
  - Errors a model can fix are raised as the SDK's `ToolError`, which reaches the
    model as is_error=True carrying the recovery path (the valid section paths,
    the available versions). Any other exception is treated as a crash and its
    message is withheld from the client, so recoverable failures must not use
    bare ValueError/LookupError.
  - Read-only, and annotated as such.
"""

from __future__ import annotations

import argparse
import os
import threading

import duckdb
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from regdocs_mcp import __version__, index, obligations
from regdocs_mcp.models import (
    DiffResult,
    ObligationOut,
    ObligationsResult,
    SearchHit,
    SearchResult,
    SectionResult,
)

MAX_TOP_K = 50
MAX_SECTION_CHARS = 8000
DEFAULT_SECTION_CHARS = 4000
OBLIGATIONS_PAGE = 50
EXTRACTOR_ID = "rule-based/modal-verb@1"

mcp = MCPServer(
    name="regdocs",
    title="Regulatory Documents",
    version=__version__,
    instructions=(
        "Search and read Singapore MAS regulatory documents (notices and guidelines). "
        "Workflow: search_notices to find relevant clauses, then use the doc_id and "
        "section_path it returns with get_document_section for full text, or "
        "list_obligations for the requirements a document imposes. Always cite the "
        "section_path — it is the clause number a compliance officer will look up."
    ),
)

# Every tool reads from a pre-built index and mutates nothing.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    idempotent_hint=True,
    destructive_hint=False,
    open_world_hint=False,
)

_conn: duckdb.DuckDBPyConnection | None = None
_lock = threading.Lock()


def _db() -> duckdb.DuckDBPyConnection:
    """Open the index once per process. Read-only, so sharing it is safe."""
    global _conn
    with _lock:
        if _conn is None:
            _conn = index.connect()
        return _conn


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "Full-text search across MAS notices and guidelines. Returns ranked clauses with "
        "their doc_id and section_path — not full text. Use get_document_section to read a "
        "result. Filter by issuer, doc_type ('notices' or 'guidelines') and date_from "
        "(ISO yyyy-mm-dd; documents that do not state an effective date are excluded)."
    ),
)
def search_notices(
    query: str,
    issuer: str | None = None,
    doc_type: str | None = None,
    date_from: str | None = None,
    top_k: int = 10,
    cursor: str | None = None,
) -> SearchResult:
    """Search regulatory clauses. Paginated via cursor."""
    if not query.strip():
        raise ToolError("query must not be empty")
    if not 1 <= top_k <= MAX_TOP_K:
        raise ToolError(f"top_k must be between 1 and {MAX_TOP_K}, got {top_k}")
    if doc_type and doc_type not in ("notices", "guidelines"):
        raise ToolError(f"doc_type must be 'notices' or 'guidelines', got {doc_type!r}")

    page = index.search_sections(
        _db(),
        query,
        issuer=issuer,
        doc_type=doc_type,
        date_from=date_from,
        top_k=top_k,
        offset=index.decode_cursor(cursor),
    )
    return SearchResult(
        hits=[SearchHit(**h) for h in page.rows],
        total=page.total,
        next_cursor=page.next_cursor,
    )


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "Read the full text of one clause, identified by the doc_id and section_path from "
        "search_notices. Long clauses are windowed: if has_more is true, call again with "
        "offset=next_offset. Returns at most 8000 characters per call."
    ),
)
def get_document_section(
    doc_id: str,
    section_path: str,
    offset: int = 0,
    max_chars: int = DEFAULT_SECTION_CHARS,
) -> SectionResult:
    """Read one clause, windowed."""
    if offset < 0:
        raise ToolError("offset must not be negative")
    if not 1 <= max_chars <= MAX_SECTION_CHARS:
        raise ToolError(f"max_chars must be between 1 and {MAX_SECTION_CHARS}, got {max_chars}")

    conn = _db()
    row = index.get_section(conn, doc_id, section_path)
    if row is None:
        # Recoverable: tell the model what it could have asked for instead.
        if index.document(conn, doc_id) is None:
            raise ToolError(f"no document {doc_id!r}. Use search_notices to obtain a valid doc_id.")
        valid = index.section_paths(conn, doc_id)
        raise ToolError(
            f"document {doc_id!r} has no section {section_path!r}. "
            f"Valid section paths include: {', '.join(valid[:20])}"
        )

    text = row["text"]
    window = text[offset : offset + max_chars]
    consumed = offset + len(window)
    return SectionResult(
        doc_id=row["doc_id"],
        section_path=row["section_path"],
        title=row["title"],
        heading=row["heading"],
        text=window,
        page_from=row["page_from"],
        page_to=row["page_to"],
        offset=offset,
        returned_chars=len(window),
        total_chars=row["char_len"],
        has_more=consumed < row["char_len"],
        next_offset=consumed if consumed < row["char_len"] else None,
    )


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "Extract the obligations a document imposes, clause by clause, each tagged as a "
        "requirement (shall/must), prohibition (shall not) or permission (may). Rule-based "
        "and deterministic — it will miss obligations spanning sentences or conditioned by "
        "an earlier clause. Paginated via cursor."
    ),
)
def list_obligations(doc_id: str, cursor: str | None = None) -> ObligationsResult:
    """Extract obligations from a document. Paginated via cursor."""
    conn = _db()
    doc = index.document(conn, doc_id)
    if doc is None:
        raise ToolError(f"no document {doc_id!r}. Use search_notices to obtain a valid doc_id.")

    found = [
        o
        for s in index.document_sections(conn, doc_id)
        for o in obligations.extract(s["section_path"], s["heading"], s["text"])
    ]
    offset = index.decode_cursor(cursor)
    page = found[offset : offset + OBLIGATIONS_PAGE]
    consumed = offset + len(page)
    return ObligationsResult(
        doc_id=doc_id,
        title=doc["title"],
        obligations=[
            ObligationOut(
                section_path=o.section_path, heading=o.heading, modality=o.modality, text=o.text
            )
            for o in page
        ],
        total=len(found),
        next_cursor=index.encode_cursor(consumed) if consumed < len(found) else None,
        extractor=EXTRACTOR_ID,
    )


@mcp.tool(
    annotations=READ_ONLY,
    description=(
        "Compare two versions of the same document and return the clauses that changed. "
        "Version history is built by re-fetching the corpus over time, so a freshly built "
        "index has one version per document and this returns an error naming the versions "
        "it does have. Call it to discover what versions exist for a doc_id."
    ),
)
def diff_versions(doc_id: str, v1: str, v2: str) -> DiffResult:
    """Diff two versions of a document."""
    conn = _db()
    doc = index.document(conn, doc_id)
    if doc is None:
        raise ToolError(f"no document {doc_id!r}. Use search_notices to obtain a valid doc_id.")

    available = index.versions(conn, doc_id)
    labels = [v["version_label"] for v in available]
    missing = [v for v in (v1, v2) if v not in labels]
    if missing:
        raise ToolError(
            f"document {doc_id!r} has no version {', '.join(repr(m) for m in missing)}. "
            f"Versions on record: {', '.join(labels) or 'none'}. "
            "This index holds one version per document; history accumulates as the corpus "
            "is re-fetched, so there is nothing to diff yet."
        )
    # Both labels resolve, but a single-version index cannot reach here with v1 != v2.
    return DiffResult(doc_id=doc_id, title=doc["title"], v1=v1, v2=v2, changes=[], total=0)


def main() -> None:
    """Console-script entry point. stdio transport; Streamable HTTP lands on Day 2.

    The index can be given as --index or as REGDOCS_INDEX. Both exist because
    hosts differ: Claude Code passes a declared `env` block through, while MCP
    Inspector spawns the server with a sanitised environment, so a server that
    can only be configured by environment variable is unreachable there.
    """
    parser = argparse.ArgumentParser(prog="regdocs-mcp", description="regdocs MCP server (stdio)")
    parser.add_argument(
        "--index",
        help=f"path to the DuckDB index (overrides ${index.DEFAULT_INDEX_ENV})",
    )
    args = parser.parse_args()
    if args.index:
        os.environ[index.DEFAULT_INDEX_ENV] = args.index
    if not os.environ.get(index.DEFAULT_INDEX_ENV):
        raise SystemExit(
            f"no index configured: pass --index PATH or set {index.DEFAULT_INDEX_ENV} "
            "(build one with: regdocs-index build --corpus <dir> --out regdocs.duckdb)"
        )
    mcp.run(transport="stdio")
