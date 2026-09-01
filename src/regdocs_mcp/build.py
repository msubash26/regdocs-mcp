"""PROVISIONAL index builder — Day 1 only.

This exists so the tool surface has real content to serve on Day 1. It is
deliberately crude: PyMuPDF page text, split on MAS's numbered-clause
convention. It does NOT handle tables, footnotes, or amendments-by-reference.

Day 3 replaces this module with the Docling pipeline in `regops-ingest`, which
writes the same three tables defined in `regdocs_mcp.index`. Nothing in
`regdocs_mcp.server` imports from here — the schema is the contract, not the
parser — so that swap should not touch the tools at all.

    regdocs-index build --corpus ../compliance-copilot/corpus --out regdocs.duckdb
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

import pymupdf

from regdocs_mcp.index import SCHEMA_SQL, connect

# A clause number leading a line: "6", "6.14", "6.14.2". MAS sets the number on
# its own line in notices and inline in some guidelines, so both shapes match.
CLAUSE_RE = re.compile(r"^[ \t]*(\d{1,2}(?:\.\d{1,3}){0,3})[ \t]*(?=$|[ \t])")
# MAS's running header/footer. A bare number is handled separately: it is a page
# number at the top or bottom of a page, but a section marker in the body, and
# only its position on the page tells them apart.
NOISE_RE = re.compile(r"^\s*Monetary Authority of Singapore\s*$", re.I)
BARE_NUM_RE = re.compile(r"^\s*\d{1,3}\s*$")
# How many leading/trailing non-empty lines of a page count as furniture.
PAGE_MARGIN_LINES = 2
MIN_SECTION_CHARS = 40
MAX_HEADING_CHARS = 80
# MAS skips numbers (a repealed paragraph leaves a gap), so a successor may jump
# a little. Anything beyond this is a footnote marker, not a clause.
MAX_CLAUSE_GAP = 3

MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
DATE_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+(\d{4})\b", re.I)
REVISED_RE = re.compile(
    r"(?:last revised on|with effect from|effective(?:\s+from)?|issue date\s*:?)\s*", re.I
)


def extract_effective_date(front_text: str) -> str | None:
    """Best-effort effective date from a document's front matter.

    MAS states it as "Last revised on 30 June 2025" or as a bare date under the
    notice number. Prefer an explicitly-qualified date; fall back to the first
    date on the page. Returns ISO yyyy-mm-dd, or None when nothing is stated —
    NULL is the honest answer and `date_from` documents that it excludes them.
    """
    head = front_text[:1500]
    qualified = REVISED_RE.search(head)
    m = DATE_RE.search(head, qualified.end() if qualified else 0) or DATE_RE.search(head)
    if not m:
        return None
    day, month, year = int(m.group(1)), MONTHS.index(m.group(2).lower()) + 1, int(m.group(3))
    if not (1 <= day <= 31 and 1900 < year < 2100):
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _plausible_successor(cur: tuple[int, ...] | None, cand: tuple[int, ...]) -> bool:
    """Is `cand` a believable next clause number after `cur`?

    This is the guard that separates real clause numbers from footnote markers.
    Footnotes restart low or jump high ("56" three pages into section 6), and a
    monotonicity check rejects them where a bare regex cannot.
    """
    if cur is None:
        return cand in {(1,), (1, 1)}
    # A child: 6 -> 6.1
    if len(cand) == len(cur) + 1 and cand[:-1] == cur and cand[-1] == 1:
        return True
    # A sibling: 6.1 -> 6.2 (allowing a small gap for repealed clauses)
    if len(cand) == len(cur) and cand[:-1] == cur[:-1]:
        return 0 < cand[-1] - cur[-1] <= MAX_CLAUSE_GAP
    # Climbing back out: 6.4.2 -> 7
    if len(cand) < len(cur) and cand[:-1] == cur[: len(cand) - 1]:
        return 0 < cand[-1] - cur[len(cand) - 1] <= MAX_CLAUSE_GAP
    return False


def _looks_like_heading(line: str) -> bool:
    """A heading is short, is not a sentence, and is not footnote prose."""
    t = line.strip()
    if not t or len(t) > MAX_HEADING_CHARS or t.endswith("."):
        return False
    letters = [c for c in t if c.isalpha()]
    if not letters:
        return False
    # MAS sets section headings in caps or title case.
    upper_ratio = sum(c.isupper() for c in letters) / len(letters)
    return upper_ratio > 0.6 or t.istitle()


def _body_lines(page_text: str) -> list[str]:
    """Non-empty lines of a page, with running header/footer furniture dropped.

    A bare number is dropped only when it sits in the page's top or bottom
    margin — there it is a page number; in the body it is a section marker.
    """
    lines = [ln.rstrip() for ln in page_text.splitlines() if ln.strip()]
    keep = []
    for i, ln in enumerate(lines):
        in_margin = i < PAGE_MARGIN_LINES or i >= len(lines) - PAGE_MARGIN_LINES
        if NOISE_RE.match(ln) or (in_margin and BARE_NUM_RE.match(ln)):
            continue
        keep.append(ln)
    return keep


def split_sections(doc: pymupdf.Document) -> list[dict]:
    """Split a document into clause-numbered sections.

    Returns dicts with section_path, heading, text, page_from, page_to. Text
    before the first numbered clause is collected under path "0" (front matter:
    title block, effective dates, table of contents).
    """
    sections: list[dict] = []
    cur = {"section_path": "0", "heading": None, "lines": [], "page_from": 0, "page_to": 0}
    cur_num: tuple[int, ...] | None = None

    for pno in range(doc.page_count):
        lines = _body_lines(doc[pno].get_text())
        for i, line in enumerate(lines):
            m = CLAUSE_RE.match(line)
            cand = tuple(int(x) for x in m.group(1).split(".")) if m else None
            if cand is not None and _plausible_successor(cur_num, cand):
                path = m.group(1)
                rest = line[m.end() :].strip()
                heading = None
                if len(cand) == 1:
                    # A bare top-level number takes the following line as its
                    # heading ("1" / "INTRODUCTION"), but only if it reads like one.
                    nxt = rest or next((n.strip() for n in lines[i + 1 :] if n.strip()), "")
                    if _looks_like_heading(nxt):
                        heading = nxt
                sections.append(cur)
                cur = {
                    "section_path": path,
                    "heading": heading,
                    "lines": [rest] if rest else [],
                    "page_from": pno,
                    "page_to": pno,
                }
                cur_num = cand
            else:
                cur["lines"].append(line.strip())
                cur["page_to"] = pno
    sections.append(cur)

    out = []
    for s in sections:
        text = re.sub(r"\n{3,}", "\n\n", "\n".join(s["lines"]).strip())
        if len(text) < MIN_SECTION_CHARS:
            continue
        out.append(
            {
                "section_path": s["section_path"],
                "heading": s["heading"],
                "text": text,
                "page_from": s["page_from"],
                "page_to": s["page_to"],
            }
        )
    return out


def build(corpus: Path, out: Path, *, limit: int | None = None, doc_type: str | None = None) -> int:
    manifest = corpus / "manifest.jsonl"
    if not manifest.exists():
        sys.exit(f"error: no manifest at {manifest}")
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    if doc_type:
        rows = [r for r in rows if r["doc_type"] == doc_type]
    if limit:
        rows = rows[:limit]

    if out.exists():
        out.unlink()
    conn = connect(out, read_only=False)
    conn.execute(SCHEMA_SQL)

    per_doc, failures = [], []
    for n, r in enumerate(rows, 1):
        pdf = corpus / r["filename"]
        if not pdf.exists():
            failures.append((r["doc_id"], "missing file"))
            continue
        try:
            with pymupdf.open(pdf) as doc:
                secs = split_sections(doc)
                effective = extract_effective_date(doc[0].get_text() if doc.page_count else "")
        except Exception as exc:  # noqa: BLE001 - one bad PDF must not stop the build
            failures.append((r["doc_id"], str(exc)[:80]))
            continue

        conn.execute(
            "INSERT OR REPLACE INTO documents VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                r["doc_id"],
                r["issuer"],
                r["doc_type"],
                r["title"],
                r.get("url"),
                r.get("source_page"),
                r.get("sha256"),
                r.get("fetched_at"),
                effective,
                len(secs),
            ],
        )
        # One version row per document today. Day 3's idempotent re-fetch is what
        # actually creates history here (ADR-012); see ADR-004.
        conn.execute(
            "INSERT OR REPLACE INTO document_versions VALUES (?,?,?,?,?)",
            [r["doc_id"], "current", r.get("sha256"), r.get("fetched_at"), r["filename"]],
        )
        seen: set[str] = set()
        for ordinal, s in enumerate(secs):
            path = s["section_path"]
            if path in seen:  # a clause number repeating (e.g. in endnotes) must not collide
                path = f"{path}#{ordinal}"
            seen.add(path)
            conn.execute(
                "INSERT OR REPLACE INTO sections VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    f"{r['doc_id']}:{path}",
                    r["doc_id"],
                    path,
                    s["heading"],
                    ordinal,
                    s["text"],
                    len(s["text"]),
                    s["page_from"],
                    s["page_to"],
                ],
            )
        per_doc.append(len(secs))
        if n % 50 == 0:
            print(f"  {n}/{len(rows)} documents", file=sys.stderr)

    conn.execute("PRAGMA create_fts_index('sections', 'section_uid', 'text', 'heading')")
    conn.close()

    median = statistics.median(per_doc) if per_doc else 0
    print(f"indexed {len(per_doc)} documents, {sum(per_doc)} sections -> {out}")
    print(
        f"sections per document: median {median}, min {min(per_doc, default=0)}, "
        f"max {max(per_doc, default=0)}"
    )
    if failures:
        print(f"{len(failures)} failed: {failures[:5]}")
    # The plan's validation gate: a median of 1 means the clause heuristic did not fire.
    if median <= 1:
        print("GATE FAILED: median sections/document <= 1 — heading heuristic did not fire.")
        return 1
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(prog="regdocs-index", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the index from a fetched corpus")
    b.add_argument("--corpus", type=Path, required=True, help="directory holding manifest.jsonl")
    b.add_argument("--out", type=Path, required=True, help="DuckDB file to write")
    b.add_argument("--limit", type=int, default=None, help="index only the first N documents")
    b.add_argument("--doc-type", default=None, help="restrict to one doc_type")
    a = ap.parse_args()
    sys.exit(build(a.corpus, a.out, limit=a.limit, doc_type=a.doc_type))
