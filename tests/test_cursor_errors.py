"""A malformed cursor must reach the model with its recovery path attached.

`index.decode_cursor` raises `ValueError("malformed cursor 'page2'; pass back a
nextCursor verbatim")`. That message was written to rescue a caller, and until
Day 8 no caller ever saw it: FastMCP withholds an **unexpected** exception's
message from the client, so a model that invented a cursor received
`Error executing tool list_obligations` and nothing else. It gave up.

This is F16 in `compliance-copilot/FAILURE_MODES.md`, and the interesting part is
not the fix. `server.py`'s own module docstring already carried the rule --
*"recoverable failures must not use bare ValueError/LookupError"* -- and the
violation shipped anyway, because the `raise` is in `index.py` and the rule binds
`server.py`. A rule stated in one module cannot be enforced in another; a test
can.

So this asserts the *channel*, not the message: `is_error` set, and the recovery
sentence present in what the client actually receives.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

# Three flavours of wrong, because they fail at three different points inside
# `decode_cursor` and only one of them was the one found in the wild.
BAD_CURSORS = [
    "page2",  # not base64 at all -- what a model invents when asked to page
    "abc",  # base64-decodable bytes that are not valid UTF-8
    "aGVsbG8",  # valid UTF-8, wrong payload: no "o:" prefix
    "bzot MQ",  # "o:" prefix with a non-integer offset
]


async def _call(client, name: str, args: dict):
    res = await client.call_tool(name, args)
    body = "\n".join(c.text for c in (res.content or []) if getattr(c, "text", None))
    return res.is_error, body


class TestMalformedCursors:
    @pytest.mark.parametrize("cursor", BAD_CURSORS)
    async def test_list_obligations_returns_the_recovery_path(self, client, cursor):
        is_error, body = await _call(
            client, "list_obligations", {"doc_id": "aaa11111", "cursor": cursor}
        )
        assert is_error, "a malformed cursor must be an error, not an empty page"
        assert "nextCursor" in body, f"the recovery path was stripped: {body!r}"
        # The pre-fix behaviour, exactly: the SDK prefixes a ToolError with
        # "Error executing tool <name>: " and appends the message, and masks an
        # unexpected exception down to the prefix alone. The bare prefix is the
        # regression, not the prefix.
        assert body.strip() != "Error executing tool list_obligations", "masked as a crash"
        assert cursor in body, "the message does not say which cursor was wrong"

    @pytest.mark.parametrize("cursor", BAD_CURSORS)
    async def test_search_notices_returns_the_recovery_path(self, client, cursor):
        is_error, body = await _call(
            client, "search_notices", {"query": "beneficial owner", "cursor": cursor}
        )
        assert is_error
        assert "nextCursor" in body, f"the recovery path was stripped: {body!r}"

    async def test_a_negative_offset_is_also_recoverable_rather_than_a_crash(self, client):
        """`decode_cursor` rejects these separately, and the same channel applies."""
        import base64

        cursor = base64.urlsafe_b64encode(b"o:-5").decode().rstrip("=")
        is_error, body = await _call(
            client, "list_obligations", {"doc_id": "aaa11111", "cursor": cursor}
        )
        assert is_error and "negative" in body


class TestGoodCursorsStillWork:
    async def test_no_cursor_is_the_first_page(self, client):
        is_error, body = await _call(client, "list_obligations", {"doc_id": "aaa11111"})
        assert not is_error and '"doc_id": "aaa11111"' in body

    async def test_an_echoed_next_cursor_round_trips(self, client):
        """The contract the error message names: pass a `nextCursor` back verbatim."""
        import json

        _, first = await _call(client, "search_notices", {"query": "a", "top_k": 1})
        cursor = json.loads(first).get("next_cursor")
        if cursor is None:
            pytest.skip("the fixture corpus fits in one page at this top_k")
        is_error, body = await _call(
            client, "search_notices", {"query": "a", "top_k": 1, "cursor": cursor}
        )
        assert not is_error, body
