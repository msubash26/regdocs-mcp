# Decisions (ADRs) — regdocs-mcp

Decisions specific to this MCP server. System-level decisions live in the
[compliance-copilot](https://github.com/msubash26/compliance-copilot) repo's `DECISIONS.md`.

---

## ADR-001 — Published as a standalone repo
**Date:** 2026-08-30 · **Status:** Accepted

**Decision.** This server ships as its own repo rather than a directory inside the copilot
monorepo, and is consumed there as an editable path dependency.

**Rationale.** It is intended to be independently useful and cloneable — the tool surface has
no dependency on the copilot. The cost is that the copilot's CI must check out both repos.

---

## ADR-002 — MCP spec revision and SDK pin
**Date:** TBD · **Status:** Open — decide on Day 1

Record here: which spec revision was targeted, which `mcp` SDK version was pinned, and why.
The spec has moved fast (2024-11-05 → 2025-03-26 → 2025-06-18 → 2025-11-25). Check
`modelcontextprotocol.io/specification` for the current revision *before* writing code.
