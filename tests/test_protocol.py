"""Tests against the JSON-RPC surface, not just the Python functions.

The distinction matters: a tool can be correct as a function and still be wrong
over the wire — a schema that does not generate, a recoverable failure that
arrives as a crash with its message stripped, a cursor that does not round-trip.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.anyio

EXPECTED_TOOLS = ["search_notices", "get_document_section", "list_obligations", "diff_versions"]


class TestToolListing:
    async def test_exposes_exactly_the_four_tools(self, client):
        names = [t.name for t in (await client.list_tools()).tools]
        assert sorted(names) == sorted(EXPECTED_TOOLS)

    async def test_order_is_deterministic(self, client):
        """The spec asks for a stable order so clients can cache the list."""
        first = [t.name for t in (await client.list_tools()).tools]
        second = [t.name for t in (await client.list_tools()).tools]
        assert first == second

    async def test_every_tool_declares_both_schemas(self, client):
        for tool in (await client.list_tools()).tools:
            assert tool.input_schema["type"] == "object", tool.name
            assert tool.output_schema is not None, f"{tool.name} returns unstructured output"

    async def test_every_tool_is_annotated_read_only(self, client):
        for tool in (await client.list_tools()).tools:
            assert tool.annotations is not None, tool.name
            assert tool.annotations.read_only_hint is True, tool.name
            assert tool.annotations.destructive_hint is False, tool.name

    async def test_every_tool_has_a_description(self, client):
        for tool in (await client.list_tools()).tools:
            assert tool.description and len(tool.description) > 40, tool.name


class TestSearchNotices:
    async def test_finds_the_relevant_clause(self, client):
        res = await client.call_tool("search_notices", {"query": "customer due diligence"})
        out = res.structured_content
        assert out["total"] > 0
        assert any(h["section_path"] == "6.1" for h in out["hits"])

    async def test_returns_ids_not_full_text(self, client):
        res = await client.call_tool("search_notices", {"query": "beneficial owner"})
        hit = res.structured_content["hits"][0]
        assert {"doc_id", "section_path"} <= hit.keys()
        # The snippet is capped; the full clause comes from get_document_section.
        assert len(hit["snippet"]) <= 320

    async def test_doc_type_filter(self, client):
        out = (
            await client.call_tool(
                "search_notices", {"query": "customer due diligence", "doc_type": "guidelines"}
            )
        ).structured_content
        assert all(h["doc_type"] == "guidelines" for h in out["hits"])

    async def test_date_from_excludes_documents_with_no_stated_date(self, client):
        out = (
            await client.call_tool("search_notices", {"query": "shall", "date_from": "2000-01-01"})
        ).structured_content
        assert all(h["effective_date"] is not None for h in out["hits"])
        assert not any(h["doc_id"] == "ccc33333" for h in out["hits"])

    async def test_cursor_round_trip_is_disjoint_and_terminates(self, client):
        seen: list[str] = []
        cursor, guard = None, 0
        while guard < 20:
            args = {"query": "shall", "top_k": 1}
            if cursor:
                args["cursor"] = cursor
            out = (await client.call_tool("search_notices", args)).structured_content
            seen += [f"{h['doc_id']}:{h['section_path']}" for h in out["hits"]]
            cursor = out["next_cursor"]
            guard += 1
            if cursor is None:
                break
        assert cursor is None, "pagination did not terminate"
        assert len(seen) == len(set(seen)), "pages overlapped"

    @pytest.mark.parametrize(
        "args",
        [
            {"query": ""},
            {"query": "x", "top_k": 0},
            {"query": "x", "top_k": 999},
            {"query": "x", "doc_type": "bogus"},
        ],
    )
    async def test_bad_arguments_are_recoverable_errors(self, client, args):
        res = await client.call_tool("search_notices", args)
        assert res.is_error
        assert res.content[0].text.strip(), "error carried no message for the model to act on"


class TestGetDocumentSection:
    async def test_returns_full_text_of_one_clause(self, client):
        out = (
            await client.call_tool(
                "get_document_section", {"doc_id": "aaa11111", "section_path": "6.2"}
            )
        ).structured_content
        assert "anonymous account" in out["text"]
        assert out["has_more"] is False
        assert out["next_offset"] is None

    async def test_long_clause_is_windowed(self, client):
        first = (
            await client.call_tool(
                "get_document_section",
                {"doc_id": "aaa11111", "section_path": "6.3", "max_chars": 100},
            )
        ).structured_content
        assert first["has_more"] is True
        assert first["returned_chars"] == 100
        second = (
            await client.call_tool(
                "get_document_section",
                {
                    "doc_id": "aaa11111",
                    "section_path": "6.3",
                    "offset": first["next_offset"],
                    "max_chars": 100,
                },
            )
        ).structured_content
        assert second["offset"] == 100
        assert second["text"] != first["text"]

    async def test_windows_reassemble_into_the_whole_clause(self, client):
        parts, offset = [], 0
        while True:
            out = (
                await client.call_tool(
                    "get_document_section",
                    {
                        "doc_id": "aaa11111",
                        "section_path": "6.3",
                        "offset": offset,
                        "max_chars": 90,
                    },
                )
            ).structured_content
            parts.append(out["text"])
            if not out["has_more"]:
                break
            offset = out["next_offset"]
        assert len("".join(parts)) == out["total_chars"]

    async def test_unknown_section_names_the_valid_paths(self, client):
        res = await client.call_tool(
            "get_document_section", {"doc_id": "aaa11111", "section_path": "99.99"}
        )
        assert res.is_error
        # The message must be actionable: it has to say what could be asked instead.
        assert "6.1" in res.content[0].text

    async def test_unknown_document_points_at_search(self, client):
        res = await client.call_tool(
            "get_document_section", {"doc_id": "nope", "section_path": "1"}
        )
        assert res.is_error
        assert "search_notices" in res.content[0].text


class TestListObligations:
    async def test_classifies_modality(self, client):
        out = (
            await client.call_tool("list_obligations", {"doc_id": "aaa11111"})
        ).structured_content
        by_path = {(o["section_path"], o["modality"]) for o in out["obligations"]}
        assert ("6.1", "requirement") in by_path
        assert ("6.2", "prohibition") in by_path

    async def test_prohibition_is_not_read_as_requirement(self, client):
        """ "shall not" must be tested before "shall", or every ban reads as a duty."""
        out = (
            await client.call_tool("list_obligations", {"doc_id": "aaa11111"})
        ).structured_content
        for o in out["obligations"]:
            if "shall not" in o["text"] or "may maintain" in o["text"]:
                assert o["modality"] == "prohibition", o["text"]

    async def test_reports_which_extractor_ran(self, client):
        out = (
            await client.call_tool("list_obligations", {"doc_id": "aaa11111"})
        ).structured_content
        assert out["extractor"], "results are not comparable without naming the extractor"

    async def test_unknown_document_is_recoverable(self, client):
        res = await client.call_tool("list_obligations", {"doc_id": "nope"})
        assert res.is_error
        assert "search_notices" in res.content[0].text


class TestDiffVersions:
    async def test_missing_version_names_what_is_available(self, client):
        res = await client.call_tool(
            "diff_versions", {"doc_id": "aaa11111", "v1": "current", "v2": "2020-01-01"}
        )
        assert res.is_error
        text = res.content[0].text
        assert "current" in text, "the error must name the versions on record"
        assert "re-fetch" in text, "the error must explain why there is nothing to diff"

    async def test_unknown_document_is_recoverable(self, client):
        res = await client.call_tool("diff_versions", {"doc_id": "nope", "v1": "a", "v2": "b"})
        assert res.is_error


class TestProtocolErrors:
    async def test_unknown_tool_is_reported_as_a_tool_error_by_this_sdk(self, client):
        """The spec and its reference SDK disagree here; this pins down what we ship.

        Spec 2026-07-28 lists "unknown tool" under *protocol* errors, i.e. a
        JSON-RPC -32602. The Python SDK instead returns is_error=True with
        "Unknown tool: <name>". We do not paper over it — a client that follows
        the spec literally and only catches protocol errors would silently treat
        a misspelt tool name as a failed call. Revisit if the SDK changes.
        """
        res = await client.call_tool("no_such_tool", {})
        assert res.is_error
        assert "no_such_tool" in res.content[0].text

    async def test_argument_schema_violations_are_recoverable(self, client):
        """Wrong argument types must reach the model with the validation detail."""
        res = await client.call_tool("search_notices", {"query": 123, "top_k": "many"})
        assert res.is_error
        assert "query" in res.content[0].text

    async def test_structured_content_matches_the_declared_output_schema(self, client):
        import jsonschema

        schemas = {t.name: t.output_schema for t in (await client.list_tools()).tools}
        calls = [
            ("search_notices", {"query": "customer due diligence"}),
            ("get_document_section", {"doc_id": "aaa11111", "section_path": "6.1"}),
            ("list_obligations", {"doc_id": "aaa11111"}),
        ]
        for name, args in calls:
            res = await client.call_tool(name, args)
            assert not res.is_error, name
            jsonschema.validate(res.structured_content, schemas[name])

    async def test_structured_results_also_carry_a_text_block(self, client):
        """The spec asks for the serialised JSON alongside, for older clients."""
        res = await client.call_tool("search_notices", {"query": "customer due diligence"})
        assert res.content and res.content[0].text
