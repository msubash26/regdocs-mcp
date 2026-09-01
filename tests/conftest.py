"""A synthetic index, so the suite never depends on the fetched corpus.

The corpus is gitignored and CI does not have it. Fixtures own their data, which
also means the expected values below are exact rather than approximate.
"""

from __future__ import annotations

import pytest
from mcp import ClientSession
from mcp.client._memory import InMemoryTransport

from regdocs_mcp import index

DOCS = [
    # doc_id, issuer, doc_type, title, effective_date
    ("aaa11111", "MAS", "notices", "Notice 626 Prevention of Money Laundering", "2024-03-28"),
    ("bbb22222", "MAS", "guidelines", "Guidelines on Risk Management", "2022-01-15"),
    ("ccc33333", "SGX", "notices", "Notice with no stated date", None),
]

SECTIONS = [
    # doc_id, section_path, heading, ordinal, text
    (
        "aaa11111",
        "1",
        "INTRODUCTION",
        0,
        "This Notice is issued under section 27B of the Monetary Authority of Singapore Act.",
    ),
    (
        "aaa11111",
        "6.1",
        None,
        1,
        "A bank shall perform customer due diligence measures when establishing business "
        "relations with any customer. A bank must identify the beneficial owner.",
    ),
    (
        "aaa11111",
        "6.2",
        None,
        2,
        "A bank shall not open an anonymous account. No bank may maintain a numbered account "
        "in a fictitious name.",
    ),
    (
        "aaa11111",
        "6.3",
        None,
        3,
        "A bank may rely on a third party to perform customer due diligence measures. " + "x" * 500,
    ),
    (
        "bbb22222",
        "1",
        "SCOPE",
        0,
        "These guidelines set out risk management expectations for customer due diligence.",
    ),
    (
        "ccc33333",
        "1",
        None,
        0,
        "An issuer shall notify the exchange of any material development without delay.",
    ),
]


@pytest.fixture(scope="session")
def anyio_backend():
    """Run the suite on asyncio only; the SDK supports trio but we do not ship it."""
    return "asyncio"


@pytest.fixture(scope="session")
def index_path(tmp_path_factory):
    """Build a small index directly, bypassing the PDF parser."""
    path = tmp_path_factory.mktemp("idx") / "test.duckdb"
    conn = index.connect(path, read_only=False)
    conn.execute(index.SCHEMA_SQL)
    for doc_id, issuer, doc_type, title, eff in DOCS:
        n = sum(1 for s in SECTIONS if s[0] == doc_id)
        conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                doc_id,
                issuer,
                doc_type,
                title,
                f"https://x/{doc_id}",
                None,
                "sha-" + doc_id,
                "2026-08-30T00:00:00+00:00",
                eff,
                n,
            ],
        )
        conn.execute(
            "INSERT INTO document_versions VALUES (?,?,?,?,?)",
            [doc_id, "current", "sha-" + doc_id, "2026-08-30T00:00:00+00:00", f"{doc_id}.pdf"],
        )
    for doc_id, path_, heading, ordinal, text in SECTIONS:
        conn.execute(
            "INSERT INTO sections VALUES (?,?,?,?,?,?,?,?,?)",
            [f"{doc_id}:{path_}", doc_id, path_, heading, ordinal, text, len(text), 0, 0],
        )
    conn.execute("PRAGMA create_fts_index('sections', 'section_uid', 'text', 'heading')")
    conn.close()
    return path


@pytest.fixture
def server(index_path, monkeypatch):
    """The real server object, pointed at the fixture index."""
    monkeypatch.setenv(index.DEFAULT_INDEX_ENV, str(index_path))
    import regdocs_mcp.server as srv

    srv._conn = None  # a previous test may have cached a connection to another index
    return srv.mcp


@pytest.fixture
async def client(server):
    """A connected MCP client session, over the in-memory transport."""
    async with (
        InMemoryTransport(server, raise_exceptions=False) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        yield session
