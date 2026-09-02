"""The same query, run twice, must return the same order.

This repo shipped `ORDER BY score DESC, effective_date DESC, doc_id, ordinal` on
Day 1 and it *looks* deterministic. It is not, and the reason is that every one
of those tie-breaks fires only on exact equality: DuckDB sums BM25 term
contributions in a parallel reduction, float addition is not associative, and two
clauses whose true scores are equal come back differing in the last bit. The
columns after `score` then never run. Measured on the real 463-document index
over 40 golden questions, **9 of 40** returned a different top-20 between runs,
and 0 of 40 with the rounding in place.

`compliance-copilot` found this first and fixed it there (its ADR-022). This is
the port, and these are the tests that keep it (ADR-008).

**On what a fixture can and cannot prove.** The copilot's original fix was
verified against a small synthetic fixture, passed, and was still wrong on the
real index -- clean hand-written data has no near-ties for jitter to disturb. So
the fixture tests below assert the *mechanism* (an exact tie is broken by the
declared columns, and rounding is what makes near-ties exact), and the test that
would actually have caught the bug runs against a real index and skips when there
is not one. A determinism test over synthetic data alone is not evidence.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from regdocs_mcp import index

# Two documents carrying the same sentence, so their BM25 scores are genuinely
# equal for a query drawn from it and the tie-break has something to decide.
TIED = [
    ("ddd44444", "MAS", "notices", "Notice A on outsourcing", "2023-01-01"),
    ("eee55555", "MAS", "notices", "Notice B on outsourcing", "2023-01-01"),
]
TIED_TEXT = "An institution shall maintain a register of all outsourcing arrangements."


@pytest.fixture(scope="session")
def tied_index(tmp_path_factory):
    path = tmp_path_factory.mktemp("tied") / "tied.duckdb"
    conn = index.connect(path, read_only=False)
    conn.execute(index.SCHEMA_SQL)
    for doc_id, issuer, doc_type, title, eff in TIED:
        conn.execute(
            "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
            [doc_id, issuer, doc_type, title, f"https://x/{doc_id}", None,
             "sha-" + doc_id, "2026-08-30T00:00:00+00:00", eff, 1],
        )  # fmt: skip
        conn.execute(
            "INSERT INTO sections VALUES (?,?,?,?,?,?,?,?,?)",
            [f"{doc_id}:1", doc_id, "1", None, 0, TIED_TEXT, len(TIED_TEXT), 0, 0],
        )
    conn.execute("PRAGMA create_fts_index('sections', 'section_uid', 'text', 'heading')")
    conn.close()
    return path


def test_an_exact_score_tie_is_broken_by_the_declared_columns(tied_index):
    """Equal text, equal date: `doc_id` decides, and it decides the same way twice."""
    conn = index.connect(tied_index)
    try:
        runs = {
            tuple(h["doc_id"] for h in index.search_sections(conn, "outsourcing register").rows)
            for _ in range(8)
        }
    finally:
        conn.close()
    assert len(runs) == 1, f"one query, {len(runs)} orderings"
    order = next(iter(runs))
    assert list(order) == sorted(order), "the tie-break is doc_id ascending"


def test_repeated_queries_agree_on_the_whole_page(index_path):
    conn = index.connect(index_path)
    try:
        runs = {
            tuple(
                (h["doc_id"], h["section_path"])
                for h in index.search_sections(conn, "customer due diligence", top_k=10).rows
            )
            for _ in range(8)
        }
    finally:
        conn.close()
    assert len(runs) == 1


def test_the_ordering_rounds_before_it_compares():
    """The mechanism, pinned. Without this the tie-breaks below it never fire.

    Asserted against the source because it cannot be observed on a fixture: the
    jitter it defeats only appears in a parallel reduction over a real corpus.
    """
    src = Path(index.__file__).read_text()
    # The source carries the f-string placeholder, not the interpolated value.
    assert "ORDER BY round(t.score, {ROUND_DP}) DESC" in src
    assert index.ROUND_DP == 9


@pytest.mark.skipif(
    not os.environ.get("REGDOCS_INDEX") or not Path(os.environ["REGDOCS_INDEX"]).exists(),
    reason="needs a real corpus index; a fixture cannot reproduce float jitter",
)
def test_a_real_index_ranks_the_same_way_every_time():
    """The test that would have caught this. Skipped in CI, which has no corpus.

    Nine of forty real questions failed this before the rounding fix. The fixture
    tests above passed throughout -- which is the point of keeping both.
    """
    queries = [
        "customer due diligence beneficial owner",
        "outsourcing arrangements register",
        "notice effective date amendment",
        "capital adequacy ratio requirement",
        "anonymous account fictitious name",
    ]
    conn = index.connect(Path(os.environ["REGDOCS_INDEX"]))
    try:
        for q in queries:
            orders = {
                tuple((h["doc_id"], h["section_path"]) for h in
                      index.search_sections(conn, q, top_k=20).rows)
                for _ in range(4)
            }  # fmt: skip
            assert len(orders) == 1, f"ranking is not reproducible for: {q!r}"
    finally:
        conn.close()
