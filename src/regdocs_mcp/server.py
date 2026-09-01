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
import sys
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING

import duckdb
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from regdocs_mcp import __version__, index, obligations
from regdocs_mcp import auth as auth_mod
from regdocs_mcp.models import (
    DiffResult,
    ObligationOut,
    ObligationsResult,
    SearchHit,
    SearchResult,
    SectionResult,
)

if TYPE_CHECKING:
    from starlette.applications import Starlette

MAX_TOP_K = 50
MAX_SECTION_CHARS = 8000
DEFAULT_SECTION_CHARS = 4000
OBLIGATIONS_PAGE = 50
EXTRACTOR_ID = "rule-based/modal-verb@1"

DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
DEFAULT_HTTP_PATH = "/mcp"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")

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


def transport_security(host: str, allow_origins: Sequence[str] = ()) -> TransportSecuritySettings:
    """DNS-rebinding settings for the HTTP transport (ADR-006).

    Spec 2026-07-28 makes `Origin` validation a MUST. The SDK auto-enables its own
    settings when the bind host is loopback, but that default allows any
    `http://localhost:*` origin — on this box that includes the LangFuse UI on
    :3000, so any page served there could drive the server from a browser. We pass
    settings explicitly instead and start the origin allowlist **empty**: a
    non-browser client (Claude Code, curl) sends no `Origin` at all and is
    unaffected, and widening is a deliberate `--allow-origin`.

    The host allowlist stays permissive on port so `--port` needs no second flag.
    """
    if host in _LOOPBACK_HOSTS:
        # Bound to loopback, so accept whichever loopback name the client dialled.
        hosts = [name for h in _LOOPBACK_HOSTS for name in (h, f"{h}:*")]
    else:
        hosts = [host, f"{host}:*"]
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=list(allow_origins),
    )


def http_app(
    *,
    path: str = DEFAULT_HTTP_PATH,
    host: str = DEFAULT_HTTP_HOST,
    port: int = DEFAULT_HTTP_PORT,
    allow_origins: Sequence[str] = (),
    auth_secret: str | None = None,
    auth_issuer: str = auth_mod.DEFAULT_ISSUER,
) -> Starlette:
    """The Streamable HTTP ASGI app — same server, second transport.

    `stateless_http=True` is a correctness requirement, not a tuning knob. Era
    routing in the SDK is by header alone, so a POST that omits
    `MCP-Protocol-Version` falls through to the legacy stateful transport, which
    mints and echoes an `Mcp-Session-Id` — reintroducing the one feature spec
    2026-07-28 deleted. Stateless serves those clients without a session. ADR-006.

    Exposed as a factory so the tests can drive it over `httpx2.ASGITransport`
    with no live port.

    Authorization is off unless `auth_secret` is given. When it is, this server
    becomes an OAuth 2.1 protected resource: RFC 9728 metadata, a `401`
    challenge naming it, and audience-validated bearer tokens (ADR-007).
    """
    # Reset unconditionally: `mcp` is a module-level singleton, so a previous
    # authenticated call would otherwise leave its settings on an app asked to be
    # unauthenticated. The factory must depend only on its arguments.
    mcp.settings.auth = None
    mcp._token_verifier = None

    if auth_secret is not None:
        resource = auth_mod.resource_uri(host, port, path)
        # MCPServer validates auth wiring in its constructor, and this module's
        # server is built at import time with the tools attached — so auth, which
        # is opt-in per invocation, is applied here. `streamable_http_app` reads
        # both of these at call time.
        mcp.settings.auth = auth_mod.auth_settings(issuer=auth_issuer, resource=resource)
        mcp._token_verifier = auth_mod.JWTVerifier(
            secret=auth_secret, issuer=auth_issuer, audience=resource
        )

    app = mcp.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        transport_security=transport_security(host, allow_origins),
        host=host,
    )
    if auth_secret is not None:
        app.add_middleware(auth_mod.ScopeChallengeMiddleware, scopes=[auth_mod.DEFAULT_SCOPE])
    return app


def main() -> None:
    """Console-script entry point: stdio (default) or Streamable HTTP.

    stdio stays the default so an existing `claude mcp add` registration keeps
    working untouched, and because stdio needs no running process. HTTP is opt-in
    via `--transport http`.

    The index can be given as --index or as REGDOCS_INDEX. Both exist because
    hosts differ: Claude Code passes a declared `env` block through, while MCP
    Inspector spawns the server with a sanitised environment, so a server that
    can only be configured by environment variable is unreachable there.
    """
    parser = argparse.ArgumentParser(
        prog="regdocs-mcp",
        description="regdocs MCP server — stdio and Streamable HTTP (spec 2026-07-28)",
    )
    parser.add_argument(
        "--index",
        help=f"path to the DuckDB index (overrides ${index.DEFAULT_INDEX_ENV})",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "http"),
        default="stdio",
        help="transport to serve on (default: stdio)",
    )
    http = parser.add_argument_group("http transport (--transport http)")
    http.add_argument(
        "--host", default=DEFAULT_HTTP_HOST, help="bind address (default: %(default)s)"
    )
    http.add_argument(
        "--port", type=int, default=DEFAULT_HTTP_PORT, help="bind port (default: %(default)s)"
    )
    http.add_argument(
        "--path", default=DEFAULT_HTTP_PATH, help="endpoint path (default: %(default)s)"
    )
    http.add_argument(
        "--auth",
        action="store_true",
        help=(
            f"require an audience-validated bearer token (scope {auth_mod.DEFAULT_SCOPE}). "
            f"Reads the signing secret from ${auth_mod.SECRET_ENV}. Off by default; "
            "stdio never uses it."
        ),
    )
    http.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        metavar="ORIGIN",
        help=(
            "browser Origin permitted to call this server; repeatable. Empty by "
            "default — non-browser clients send no Origin and do not need it."
        ),
    )
    args = parser.parse_args()

    if args.index:
        os.environ[index.DEFAULT_INDEX_ENV] = args.index
    if not os.environ.get(index.DEFAULT_INDEX_ENV):
        raise SystemExit(
            f"no index configured: pass --index PATH or set {index.DEFAULT_INDEX_ENV} "
            "(build one with: regdocs-index build --corpus <dir> --out regdocs.duckdb)"
        )

    if args.transport == "stdio":
        # Spec 2026-07-28: stdio implementations SHOULD NOT use the OAuth flow and
        # should take credentials from the environment. --auth is HTTP-only.
        if args.auth:
            raise SystemExit(
                "--auth applies to --transport http only. A stdio server is a "
                "subprocess of its client and inherits that client's trust."
            )
        mcp.run(transport="stdio")
        return

    secret = None
    if args.auth:
        secret = os.environ.get(auth_mod.SECRET_ENV)
        if not secret:
            raise SystemExit(
                f"--auth needs a signing secret: set {auth_mod.SECRET_ENV}. "
                "Mint a matching dev token with: python -m regdocs_mcp.auth"
            )

    if args.host not in _LOOPBACK_HOSTS:
        # Spec 2026-07-28: servers SHOULD bind only localhost. Not refused — a
        # container needs 0.0.0.0 — but it should never be silent.
        print(
            f"warning: binding {args.host}, not loopback. The spec's DNS-rebinding "
            "guidance assumes localhost; put a reverse proxy in front and enable auth.",
            file=sys.stderr,
        )
    # Served through `http_app()` rather than `mcp.run(transport=...)` so the
    # process serves byte-identical wiring to the one the tests drive.
    import uvicorn

    try:
        app = http_app(
            path=args.path,
            host=args.host,
            port=args.port,
            allow_origins=args.allow_origin,
            auth_secret=secret,
            auth_issuer=os.environ.get(auth_mod.ISSUER_ENV, auth_mod.DEFAULT_ISSUER),
        )
    except ValueError as exc:
        # Misconfiguration, not a crash — say so in one line and exit.
        raise SystemExit(str(exc)) from exc

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )
