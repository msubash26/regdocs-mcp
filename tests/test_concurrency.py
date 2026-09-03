"""Parallel tool calls on one session must not read each other's results.

`compliance-copilot`'s Day 7 supervisor fans four sub-agents out over four
documents, and each branch calls `list_obligations`. On the real 463-document
index that reported a valid `doc_id` as missing on 2 of 4, 1 of 4 and 1 of 4
calls across three trials, and 0 of 4 when the same calls were made one after
another (ADR-009).

The cause was one `DuckDBPyConnection` shared by every handler, with a comment
saying that was safe because the connection is read-only. Read-only protects the
*file*; it does nothing for the connection's single statement context, and
FastMCP runs handlers concurrently.

What makes this worth a test rather than a fix and a note: the failure is silent
and it inverts. A valid identifier comes back as "no document", so a caller does
not see an error to retry -- it sees an authoritative answer that the corpus does
not contain something it does contain.
"""

from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.anyio

# Every document in the fixture index, so each concurrent call has a *different*
# correct answer. Firing the same call N times would pass even with the results
# crossed, which is exactly how this bug survived a test suite once already.
DOC_IDS = ["aaa11111", "bbb22222", "ccc33333"]

ROUNDS = 8


async def _text(client, name: str, args: dict) -> str:
    res = await client.call_tool(name, args)
    body = "\n".join(c.text for c in (res.content or []) if getattr(c, "text", None))
    return f"ERROR:{body}" if res.is_error else body


class TestConcurrentToolCalls:
    async def test_list_obligations_in_parallel_never_loses_a_document(self, client):
        """The reproduction, run enough times that an interleaving has to show."""
        for _ in range(ROUNDS):
            out = await asyncio.gather(
                *(_text(client, "list_obligations", {"doc_id": d}) for d in DOC_IDS)
            )
            errors = [d for d, body in zip(DOC_IDS, out, strict=True) if body.startswith("ERROR")]
            assert not errors, f"valid doc_ids reported missing under concurrency: {errors}"

    async def test_each_parallel_call_gets_its_own_answer(self, client):
        """Not just 'no errors' -- the right result reached the right caller.

        A shared statement context fails both ways: a call can raise on someone
        else's row, or succeed with it. The second is worse and an error count
        would not catch it.
        """
        for _ in range(ROUNDS):
            out = await asyncio.gather(
                *(_text(client, "list_obligations", {"doc_id": d}) for d in DOC_IDS)
            )
            for doc_id, body in zip(DOC_IDS, out, strict=True):
                assert f'"doc_id": "{doc_id}"' in body, f"{doc_id} received another call's result"

    async def test_a_mixed_parallel_workload_stays_consistent(self, client):
        """Different tools at once, which is what an agent's fan-out actually does."""
        for _ in range(ROUNDS):
            search, section, obligations = await asyncio.gather(
                _text(client, "search_notices", {"query": "beneficial owner", "top_k": 3}),
                _text(
                    client, "get_document_section", {"doc_id": "aaa11111", "section_path": "6.1"}
                ),
                _text(client, "list_obligations", {"doc_id": "bbb22222"}),
            )
            assert not search.startswith("ERROR"), search[:200]
            assert "beneficial owner" in section, section[:200]
            assert '"doc_id": "bbb22222"' in obligations, obligations[:200]
