# regdocs-mcp

An MCP server exposing regulatory-document tools (MAS notices, SGX rulebooks) over
both **stdio** and **Streamable HTTP** transports.

**Status:** Day 0 — scaffold.

| Tool | Signature |
|---|---|
| `search_notices` | `(query, issuer?, date_from?, top_k)` → ranked snippets with stable IDs |
| `get_document_section` | `(doc_id, section_path)` → full text of one section |
| `list_obligations` | `(doc_id)` → structured obligations extracted from a notice |
| `diff_versions` | `(doc_id, v1, v2)` → changed clauses |

MCP spec revision targeted and SDK version pinned: _TBD on Day 1._
