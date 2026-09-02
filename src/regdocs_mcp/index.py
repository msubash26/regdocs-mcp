"""The index contract: schema, connection, and cursor helpers.

This module is the boundary between the tool surface and whatever produced the
data. `regdocs_mcp.build` ships a provisional PyMuPDF-based builder; the Day 3
Docling pipeline in `regops-ingest` is expected to replace it by writing these
same three tables. The tools import from here and never touch a parser, so
content quality can improve without the tool signatures moving.

Located by the REGDOCS_INDEX environment variable.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass
from pathlib import Path

import duckdb

DEFAULT_INDEX_ENV = "REGDOCS_INDEX"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id      VARCHAR PRIMARY KEY,
    issuer      VARCHAR NOT NULL,
    doc_type    VARCHAR NOT NULL,
    title       VARCHAR NOT NULL,
    url         VARCHAR,
    source_page VARCHAR,
    sha256         VARCHAR,
    fetched_at     VARCHAR,
    effective_date DATE,          -- best-effort; NULL when not stated in the document
    n_sections     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sections (
    section_uid  VARCHAR PRIMARY KEY,   -- doc_id || ':' || section_path
    doc_id       VARCHAR NOT NULL,
    section_path VARCHAR NOT NULL,      -- dotted clause number, e.g. "6.14"
    heading      VARCHAR,
    ordinal      INTEGER NOT NULL,      -- document order, for stable paging
    text         VARCHAR NOT NULL,
    char_len     INTEGER NOT NULL,
    page_from    INTEGER,
    page_to      INTEGER
);

CREATE TABLE IF NOT EXISTS document_versions (
    doc_id        VARCHAR NOT NULL,
    version_label VARCHAR NOT NULL,     -- caller-facing handle, e.g. "2024-03-28"
    sha256        VARCHAR,
    fetched_at    VARCHAR,
    filename      VARCHAR,
    PRIMARY KEY (doc_id, version_label)
);

CREATE INDEX IF NOT EXISTS sections_doc_ordinal ON sections (doc_id, ordinal);
"""


class IndexUnavailable(RuntimeError):
    """The index is missing or unusable. Carries an actionable message."""


def index_path() -> Path:
    """Resolve the index location from the environment."""
    raw = os.environ.get(DEFAULT_INDEX_ENV)
    if not raw:
        raise IndexUnavailable(
            f"{DEFAULT_INDEX_ENV} is not set. Point it at a regdocs index, e.g. "
            f"{DEFAULT_INDEX_ENV}=/path/to/regdocs.duckdb. Build one with: regdocs-index build"
        )
    return Path(raw).expanduser()


def connect(path: Path | None = None, *, read_only: bool = True) -> duckdb.DuckDBPyConnection:
    """Open the index. Read-only by default — every tool is a reader."""
    p = path or index_path()
    if read_only and not p.exists():
        raise IndexUnavailable(f"no index at {p}. Build one with: regdocs-index build")
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(p), read_only=read_only)
    conn.execute("INSTALL fts; LOAD fts;")
    return conn


# ---------------------------------------------------------------------------
# Cursors. Opaque to the caller by contract, so the encoding can change later
# without breaking clients that only ever echo them back.
# ---------------------------------------------------------------------------


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"o:{offset}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        pad = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(cursor + pad).decode()
        if not raw.startswith("o:"):
            raise ValueError(raw)
        offset = int(raw[2:])
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise ValueError(f"malformed cursor {cursor!r}; pass back a nextCursor verbatim") from exc
    if offset < 0:
        raise ValueError("cursor offset must not be negative")
    return offset


@dataclass(frozen=True)
class Page:
    """One page of results plus the cursor that continues it."""

    rows: list[dict]
    next_cursor: str | None
    total: int


# ---------------------------------------------------------------------------
# Queries. The tools call these; nothing here knows how the text was produced.
# ---------------------------------------------------------------------------

SNIPPET_CHARS = 320

# Decimal places a BM25 score is rounded to before it is ordered.
#
# `ORDER BY score DESC, effective_date DESC, doc_id, ordinal` looks deterministic
# and is not, because those tie-breaks only fire on *exact* equality. DuckDB sums
# each term's BM25 contribution in a parallel reduction, floating-point addition
# is not associative, and the same query returns the same score varying in its
# last bit -- so two clauses whose true scores are equal compare as unequal and
# the tie-break never runs. Measured on the real index over 40 golden questions,
# **9 of 40** returned a different top-20 between runs, and **0 of 40** after.
#
# Rounding first collapses the jitter into a real tie, which the columns after it
# then break. 9 places sits ~6 orders of magnitude above the observed jitter
# (~1e-15 at these magnitudes) and far below any score difference that carries
# meaning. Ported from compliance-copilot ADR-022; see ADR-008 here.
ROUND_DP = 9


def search_sections(
    conn: duckdb.DuckDBPyConnection,
    query: str,
    *,
    issuer: str | None = None,
    doc_type: str | None = None,
    date_from: str | None = None,
    top_k: int = 10,
    offset: int = 0,
) -> Page:
    """BM25 search over section text, newest-first within equal relevance."""
    where = ["score IS NOT NULL"]
    params: list[object] = [query]
    if issuer:
        where.append("d.issuer = ?")
        params.append(issuer)
    if doc_type:
        where.append("d.doc_type = ?")
        params.append(doc_type)
    if date_from:
        # Documents with no stated effective date are excluded, not assumed recent.
        where.append("d.effective_date IS NOT NULL AND d.effective_date >= ?")
        params.append(date_from)
    clause = " AND ".join(where)

    total = conn.execute(
        f"""SELECT count(*) FROM (
              SELECT fts_main_sections.match_bm25(s.section_uid, ?) AS score, s.*, d.*
              FROM sections s JOIN documents d USING(doc_id)
            ) t JOIN documents d USING(doc_id) WHERE {clause}""",
        params,
    ).fetchone()[0]

    rows = conn.execute(
        f"""SELECT t.doc_id, t.section_path, d.title, d.doc_type, t.heading,
                   CAST(d.effective_date AS VARCHAR), t.score, substr(t.text, 1, {SNIPPET_CHARS})
            FROM (SELECT fts_main_sections.match_bm25(s.section_uid, ?) AS score, s.*
                  FROM sections s) t
            JOIN documents d USING(doc_id)
            WHERE {clause}
            ORDER BY round(t.score, {ROUND_DP}) DESC, d.effective_date DESC NULLS LAST,
                     t.doc_id, t.ordinal
            LIMIT ? OFFSET ?""",
        [*params, top_k, offset],
    ).fetchall()

    keys = (
        "doc_id",
        "section_path",
        "title",
        "doc_type",
        "heading",
        "effective_date",
        "score",
        "snippet",
    )
    hits = [dict(zip(keys, r, strict=True)) for r in rows]
    consumed = offset + len(hits)
    return Page(hits, encode_cursor(consumed) if consumed < total else None, total)


def get_section(conn: duckdb.DuckDBPyConnection, doc_id: str, section_path: str) -> dict | None:
    row = conn.execute(
        """SELECT s.doc_id, s.section_path, d.title, s.heading, s.text,
                  s.page_from, s.page_to, s.char_len
           FROM sections s JOIN documents d USING(doc_id)
           WHERE s.doc_id = ? AND s.section_path = ?""",
        [doc_id, section_path],
    ).fetchone()
    if not row:
        return None
    keys = (
        "doc_id",
        "section_path",
        "title",
        "heading",
        "text",
        "page_from",
        "page_to",
        "char_len",
    )
    return dict(zip(keys, row, strict=True))


def document(conn: duckdb.DuckDBPyConnection, doc_id: str) -> dict | None:
    row = conn.execute(
        "SELECT doc_id, title, doc_type, issuer, CAST(effective_date AS VARCHAR), n_sections "
        "FROM documents WHERE doc_id = ?",
        [doc_id],
    ).fetchone()
    keys = ("doc_id", "title", "doc_type", "issuer", "effective_date", "n_sections")
    return dict(zip(keys, row, strict=True)) if row else None


def section_paths(conn: duckdb.DuckDBPyConnection, doc_id: str, limit: int = 40) -> list[str]:
    """Valid section paths for a document — used to make a bad path recoverable."""
    return [
        r[0]
        for r in conn.execute(
            "SELECT section_path FROM sections WHERE doc_id = ? ORDER BY ordinal LIMIT ?",
            [doc_id, limit],
        ).fetchall()
    ]


def document_sections(conn: duckdb.DuckDBPyConnection, doc_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT section_path, heading, text FROM sections WHERE doc_id = ? ORDER BY ordinal",
        [doc_id],
    ).fetchall()
    return [dict(zip(("section_path", "heading", "text"), r, strict=True)) for r in rows]


def versions(conn: duckdb.DuckDBPyConnection, doc_id: str) -> list[dict]:
    rows = conn.execute(
        """SELECT v.version_label, CAST(d.effective_date AS VARCHAR), v.sha256, v.fetched_at
           FROM document_versions v LEFT JOIN documents d USING(doc_id)
           WHERE v.doc_id = ? ORDER BY v.version_label""",
        [doc_id],
    ).fetchall()
    keys = ("version_label", "effective_date", "sha256", "fetched_at")
    return [dict(zip(keys, r, strict=True)) for r in rows]
