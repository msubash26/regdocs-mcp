"""Deterministic obligation extraction from regulatory clause text.

Rule-based on purpose. MAS drafts obligations in a narrow, consistent register
("a bank shall...", "no person may..."), so a modal-verb rule captures most of
them without a model in the loop. That makes this cheap, reproducible, and — the
point — a *baseline*: when an LLM extractor arrives it has something to beat, on
the same corpus, with a number attached.

Known limits, worth stating because they are what an LLM would be asked to fix:
obligations spanning two sentences, conditional obligations whose trigger sits
in a preceding clause, and cross-references ("as set out in paragraph 6.14").
Enumerated limbs are kept attached to their stem, so a single obligation can be
long; that is the faithful reading, not a bug.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Ordered: the first pattern to match a sentence names its modality. "shall not"
# has to be tested before "shall", or every prohibition reads as a requirement.
MODALITIES: list[tuple[str, re.Pattern[str]]] = [
    ("prohibition", re.compile(r"\b(?:shall|must|may)\s+not\b|\bno\s+\w+\s+(?:shall|may)\b", re.I)),
    ("requirement", re.compile(r"\b(?:shall|must)\b|\bis\s+required\s+to\b", re.I)),
    ("permission", re.compile(r"\bmay\b", re.I)),
]
# Split on a full stop followed by a capital only. Deliberately NOT on ";" and
# not before "(a)": MAS drafts a single obligation with enumerated limbs
# ("the bank shall - (a) ...; (b) ..."), and splitting there detaches the limbs
# from the stem that carries the duty, turning one obligation into several
# fragments that no longer say who must do what.
SENTENCE_RE = re.compile(r"(?<=\.)\s+(?=[A-Z])")
MIN_OBLIGATION_CHARS = 25
# A clause that only cross-references carries no obligation of its own.
CROSS_REF_ONLY_RE = re.compile(r"^\s*(?:refer to|see)\b", re.I)


@dataclass(frozen=True)
class Obligation:
    section_path: str
    heading: str | None
    modality: str
    text: str


def classify(sentence: str) -> str | None:
    """Name the modality of a sentence, or None if it states no obligation."""
    for name, pattern in MODALITIES:
        if pattern.search(sentence):
            return name
    return None


def extract(section_path: str, heading: str | None, text: str) -> list[Obligation]:
    """Pull obligation-bearing sentences out of one section's text."""
    found: list[Obligation] = []
    # PDF text arrives hard-wrapped; rejoin before splitting on sentences.
    flowed = re.sub(r"\s*\n\s*", " ", text).strip()
    for raw in SENTENCE_RE.split(flowed):
        sentence = raw.strip()
        if len(sentence) < MIN_OBLIGATION_CHARS or CROSS_REF_ONLY_RE.match(sentence):
            continue
        modality = classify(sentence)
        if modality:
            found.append(Obligation(section_path, heading, modality, sentence))
    return found
