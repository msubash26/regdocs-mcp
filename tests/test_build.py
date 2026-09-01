"""Unit tests for the provisional splitter.

These guard the two heuristics that were wrong on the first pass: footnote
markers read as clause numbers, and page numbers eating real section markers.
Both cost real debugging time, so they get regression cover even though this
module is scheduled for replacement on Day 3.
"""

from __future__ import annotations

import pytest

from regdocs_mcp.build import (
    _body_lines,
    _looks_like_heading,
    _plausible_successor,
    extract_effective_date,
)


class TestClauseSuccession:
    @pytest.mark.parametrize(
        ("cur", "cand"),
        [
            (None, (1,)),  # first clause of a document
            (None, (1, 1)),
            ((6,), (6, 1)),  # descend into sub-clauses
            ((6, 1), (6, 2)),  # sibling
            ((6, 1), (6, 4)),  # sibling across a repealed clause
            ((6, 4, 2), (7,)),  # climb back out
        ],
    )
    def test_accepts_real_clause_numbers(self, cur, cand):
        assert _plausible_successor(cur, cand)

    @pytest.mark.parametrize(
        ("cur", "cand"),
        [
            ((6,), (56,)),  # a footnote marker, the original bug
            ((6, 1), (1,)),  # a footnote restarting low
            ((6, 1), (6, 9)),  # too big a jump to be a repealed clause
            (None, (7,)),  # a document does not begin at clause 7
            ((6,), (11,)),  # gap larger than MAX_CLAUSE_GAP is suspicious
        ],
    )
    def test_rejects_footnote_markers(self, cur, cand):
        assert not _plausible_successor(cur, cand)


class TestPageFurniture:
    def test_bare_number_in_the_margin_is_a_page_number(self):
        page = "12\nMonetary Authority of Singapore\nSome body text here.\nMore body.\n7\n"
        assert "12" not in _body_lines(page)
        assert "7" not in _body_lines(page)

    def test_bare_number_in_the_body_is_a_section_marker(self):
        page = "header line\nanother header\n6\nCUSTOMER DUE DILIGENCE\nbody\nfooter\nfooter2\n"
        assert "6" in _body_lines(page)

    def test_running_footer_is_always_dropped(self):
        page = "a\nb\nMonetary Authority of Singapore\nc\nd\n"
        assert not any("Monetary Authority" in ln for ln in _body_lines(page))


class TestHeadingDetection:
    @pytest.mark.parametrize("line", ["INTRODUCTION", "CUSTOMER DUE DILIGENCE", "Scope Of Notice"])
    def test_accepts_headings(self, line):
        assert _looks_like_heading(line)

    @pytest.mark.parametrize(
        "line",
        [
            "This Notice is issued under section 27B of the Act.",  # a sentence
            "Money laundering includes proliferation financing, and all references in these",
            "",
            "1234",
        ],
    )
    def test_rejects_prose_and_footnote_text(self, line):
        assert not _looks_like_heading(line)


class TestEffectiveDate:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("MAS Notice 626\n28 March 2024\nLast revised on 30 June 2025", "2025-06-30"),
            ("Notice No : MAS 501\nIssue Date : 16 April 2020", "2020-04-16"),
            ("Guidelines\n1 August 2024\nsomething else", "2024-08-01"),
            ("No date anywhere in this front matter", None),
        ],
    )
    def test_prefers_the_qualified_date(self, text, expected):
        assert extract_effective_date(text) == expected

    def test_rejects_an_implausible_year(self):
        assert extract_effective_date("issued 12 March 1823") is None


class TestObligationExtraction:
    """The enumerated-limb case, found by the Day 1 demo itself."""

    STEM = (
        "For the purposes of identifying the beneficial owners, the bank shall - "
        "(a) for customers that are legal persons, identify the natural persons; "
        "(b) for customers that are legal arrangements, identify the trustees."
    )

    def test_enumerated_limbs_stay_attached_to_their_stem(self):
        from regdocs_mcp.obligations import extract

        found = extract("6.14", None, self.STEM)
        assert len(found) == 1, "splitting on ';' detaches limbs from the duty-bearing stem"
        assert "(a)" in found[0].text and "(b)" in found[0].text

    def test_separate_sentences_are_separate_obligations(self):
        from regdocs_mcp.obligations import extract

        text = "A bank shall identify the customer. A bank must verify their identity."
        assert len(extract("6.1", None, text)) == 2

    def test_prohibition_beats_requirement(self):
        from regdocs_mcp.obligations import extract

        found = extract("6.2", None, "A bank shall not open an anonymous account.")
        assert found[0].modality == "prohibition"

    def test_clause_with_no_modal_yields_nothing(self):
        from regdocs_mcp.obligations import extract

        assert extract("2.1", None, 'In this Notice, "bank" means a bank in Singapore.') == []
