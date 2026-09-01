"""Tool output models.

These generate each tool's `outputSchema`, so a client can validate what comes
back rather than parsing prose. Field descriptions are part of the wire contract
and are read by the model calling the tool — they are worth writing carefully.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SearchHit(BaseModel):
    doc_id: str = Field(description="Stable document ID. Pass to get_document_section.")
    section_path: str = Field(description='Clause number, e.g. "6.14". Cite this.')
    title: str = Field(description="Document title.")
    doc_type: str = Field(description='"notices" or "guidelines".')
    heading: str | None = Field(
        default=None, description="Section heading, when the document has one."
    )
    effective_date: str | None = Field(
        default=None, description="ISO date, or null if the document does not state one."
    )
    score: float = Field(description="BM25 relevance. Comparable within one result set only.")
    snippet: str = Field(
        description="Opening of the section. Use get_document_section for the full text."
    )


class SearchResult(BaseModel):
    hits: list[SearchHit]
    total: int = Field(description="Total matching sections, not just this page.")
    next_cursor: str | None = Field(
        default=None, description="Pass back as `cursor` for the next page. Null when exhausted."
    )


class SectionResult(BaseModel):
    doc_id: str
    section_path: str
    title: str
    heading: str | None = None
    text: str = Field(description="Section text for the requested window.")
    page_from: int | None = None
    page_to: int | None = None
    offset: int = Field(description="Character offset this window starts at.")
    returned_chars: int
    total_chars: int = Field(description="Full length of the section.")
    has_more: bool
    next_offset: int | None = Field(
        default=None, description="Pass as `offset` to continue. Null at the end."
    )


class ObligationOut(BaseModel):
    section_path: str
    heading: str | None = None
    modality: str = Field(
        description='"requirement" (shall/must), "prohibition" (shall not), or "permission" (may).'
    )
    text: str


class ObligationsResult(BaseModel):
    doc_id: str
    title: str
    obligations: list[ObligationOut]
    total: int
    next_cursor: str | None = None
    extractor: str = Field(
        description="Which extractor produced these, so results stay comparable across versions."
    )


class VersionOut(BaseModel):
    version_label: str = Field(description="Pass as v1 or v2 to diff_versions.")
    effective_date: str | None = None
    sha256: str | None = None
    fetched_at: str | None = None


class ClauseChange(BaseModel):
    section_path: str
    change_type: str = Field(description='"added", "removed" or "modified".')
    before: str | None = None
    after: str | None = None


class DiffResult(BaseModel):
    doc_id: str
    title: str
    v1: str
    v2: str
    changes: list[ClauseChange]
    total: int
    next_cursor: str | None = None
