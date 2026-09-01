# Day 1 demo — three tools in sequence

The Day 1 "done when": ask a real compliance question in an MCP host and watch the
model chain the tools. Run against the full 463-document MAS index through Claude Code.

**Question**

> What are a bank's customer due diligence obligations under MAS Notice 626, and what
> guidance expands on them?

**Tool-call sequence**

Tool calls: 14 across 3 distinct tools, 79s wall clock

```
 1. search_notices(query='customer due diligence', top_k=10)
 2. get_document_section(doc_id='1b9b9f6db2876069', section_path='6', max_chars=8000)
 3. search_notices(query='Guidelines to Notice 626 banks money laundering', doc_type='guidelines', top_k=10)
 4. get_document_section(doc_id='1b9b9f6db2876069', section_path='7', max_chars=8000)
 5. search_notices(query='bank shall perform customer due diligence measures when establishing business relations identify customer verify identity beneficial owner', doc_type='notices', top_k=10)
 6. get_document_section(doc_id='1b9b9f6db2876069', section_path='6.1', max_chars=8000)
 7. get_document_section(doc_id='1b9b9f6db2876069', section_path='6.6', max_chars=8000)
 8. get_document_section(doc_id='1b9b9f6db2876069', section_path='6.14', max_chars=8000)
 9. get_document_section(doc_id='1b9b9f6db2876069', section_path='6.5', max_chars=8000)
10. list_obligations(doc_id='1b9b9f6db2876069')
11. list_obligations(doc_id='1b9b9f6db2876069', cursor='bzo1MA')
12. get_document_section(doc_id='a0a676334344e6eb', section_path='14', max_chars=6000)
13. get_document_section(doc_id='a0a676334344e6eb', section_path='15', max_chars=3000)
14. get_document_section(doc_id='a0a676334344e6eb', section_path='16', max_chars=3000)
```

## What this run showed

**The tool contract held under a real agent loop.** Every hop used the `doc_id` and
`section_path` returned by the previous call — the model never needed a raw document
dump, which is the whole point of returning stable IDs instead of blobs.

**Pagination was exercised without being asked for.** Call 11 continues call 10 with the
cursor call 10 returned. The model discovered the second page on its own from
`next_cursor`, which is the behaviour the pagination contract is meant to produce.

**Search-then-narrow, not search-once.** Three separate `search_notices` calls with
progressively more specific queries, interleaved with section reads. Worth remembering
when Day 5 benchmarks retrieval: in practice the agent issues several queries per
question, so per-query latency is multiplied by roughly 3 in a real answer.

**The demo found a bug in the extractor.** The model noted unprompted that
`list_obligations` was truncating enumerated limbs — `6.3` rendered only limb (a). The
cause was splitting sentences on `;`, which detached limbs from the stem carrying the
duty ("the bank shall – (a) ...; (b) ..."). Fixed, and `tests/test_build.py` now guards
it. An agent reading its own tool output is a usable review signal.

## Full answer

## Answer

**Primary instrument:** *Notice 626 — Prevention of Money Laundering and Countering the Financing of Terrorism – Banks* (`doc_id 1b9b9f6db2876069`, effective 2025-06-30). CDD sits in **paragraph 6**, with paragraph 7 (simplified CDD) and paragraph 8 (enhanced CDD) applying on top.

**Expanding guidance:** *Guidelines to Notice 626* (`doc_id a0a676334344e6eb`, 28 Mar 2024, last revised 1 Jul 2025). Per `a0a676334344e6eb §1` these "provide guidance to all banks on the requirements in MAS Notice 626… should be read in conjunction with the Notice." Its CDD commentary runs from `§14` onward, keyed clause-by-clause to the Notice.

### A bank's CDD obligations under Notice 626

**Threshold prohibitions**
- `626 §6.1` — no anonymous or fictitious-name accounts (prohibition).
- `626 §6.2` — where there are reasonable grounds to suspect proceeds of crime (CDSA) or terrorism-financing property (TSOFA) *before* relations begin: do not establish relations or transact, and file an STR.
- `626 §6.35` — if CDD under paras 6, 7 and 8 cannot be completed: do not commence or continue relations, and consider filing an STR.

**When CDD is triggered** (`626 §6.3`) — establishing business relations; occasional transactions above S$20,000; *any* digital token transaction; domestic wire transfers or cross-border transfers above S$1,500; any value transfer; any ML/TF suspicion; or doubts about previously obtained information. `§6.4` requires aggregating deliberately structured linked transactions.

**The CDD measures themselves**
- Identify the customer and obtain the prescribed data set — `626 §6.6` (natural person: full name/aliases, unique ID number, residential address, DOB, nationality; legal person or arrangement: name, registration number, addresses, date and place of incorporation, trust deed, purpose, place of administration, legal form and powers).
- Connected parties of a legal person/arrangement — `626 §6.8`, with a not-high-risk fallback to DOB and nationality at `§6.8A` and a documentation duty at `§6.8B`.
- Verify identity from reliable, independent source data — `626 §6.9` (including legal form, proof of existence, constitution and powers for entities).
- Persons appointed to act for the customer — identify and verify (`626 §6.10`) and verify their due authority (`626 §6.11`).
- Beneficial owners — inquire whether any exist (`626 §6.13`), identify them through the ownership → control → executive-authority cascade, and for trusts identify trust relevant parties and the whole chain of control (`626 §6.14`); obtain the prescribed particulars (`§6.14A`); verify by reasonable steps (`§6.14B`); low-risk data substitutions (`§6.14C`) must be documented (`§6.14D`). Exemptions for listed entities, Appendix 1 FIs and FATF-supervised FIs at `626 §6.16`, with the basis documented under `§6.17`.
- Understand the entity's business and ownership/control structure — `626 §6.15`.
- Understand and obtain the purpose and intended nature of the relationship — `626 §6.18`.
- Life-policy beneficiaries where the bank distributes for a licensed life insurer — `626 §6.17B`, `§6.17C`.

**Ongoing monitoring** — monitor relations continuously (`626 §6.19`); scrutinise transactions against the customer's profile and source of funds (`626 §6.20`, with COSMIC information folded in); pay special attention to complex, unusually large or unusual patterns lacking economic purpose (`626 §6.21`); maintain adequate systems and processes to detect and report them (`626 §6.22`); inquire into and document background and purpose (`626 §6.23`); keep CDD data current, especially for higher-risk customers (`626 §6.24`). Retaining a suspicious customer requires documented reasons plus commensurate mitigation (`626 §6.25`) and, if higher risk, enhanced CDD with senior-management approval (`626 §6.26`).

**Timing, channels and special cases** — non-face-to-face policies (`626 §§6.27–6.29`, CDD "at least as robust"); business acquisitions (`626 §6.30`); occasional transactions treated as if relations were established, with reconstructable records (`626 §6.31`, digital tokens at `§6.31A`); verification before relations/transactions (`626 §6.32`), deferral permitted only where essential to business continuity and risks are managed (`626 §6.33`), subject to internal policies and prompt completion (`626 §6.34`); joint accounts treated as individual customers (`626 §6.37`); re-application to existing customers on revision of the Notice (`626 §6.38`).

**Screening** — screen customers, appointed persons, connected parties and beneficial owners against ML/TF sources and authority-provided lists (`626 §6.39`), at the events listed in `§6.40` (including periodically and on list changes), plus wire-transfer and value-transfer parties (`§6.41`, `§6.41A`); document results (`626 §6.42`).

**Simplified CDD (para 7)** — permitted only on a documented low-risk assessment (`626 §§7.1–7.3`, `§7.6`), never for FATF countermeasure jurisdictions, inadequate-AML/CFT jurisdictions, or where ML/TF is suspected (`626 §7.4`).

**Enhanced CDD (para 8)** — systems to detect PEPs, their family members and close associates (`626 §8.2`); for PEP customers/beneficial owners, senior-management approval, establishing source of wealth and source of funds, and enhanced monitoring (`626 §8.3`); risk-based approach available for domestic and international-organisation PEPs and steppers-down (`626 §8.4`); systems to identify higher-risk relations generally (`626 §8.5`), with the higher-risk circumstances at `§8.6` and mandatory EDD at `§8.7`.

**Reliance on third parties** — permitted under `626 §9.2` conditions, but never for ongoing monitoring (`626 §9.3`), with documentation and immediate transfer of CDD information (`626 §9.4`) and undiminished responsibility (`626 §9.5`).

### What the Guidelines add
- `Guidelines §14` — COSMIC information must feed the paras 6/7/8 measures; risk-assessment expectations for the `§6.2` suspicion decision; how to spot linked/restructured transactions under `§6.4`.
- `Guidelines §15` — `§6.3(f)–(g)` triggers apply regardless of whether suspicion arose via COSMIC, and re-trigger on new COSMIC information; on `§§6.5–6.18`, verification documents should be those hardest to counterfeit and rigour should scale to the customer's risk profile; banks should collect background beyond `§6.6` (occupation, employer, nature of business, income range, related accounts, prominent public function).
- `Guidelines §16` — P.O. box addresses only where a residential address is unavailable locally; obtain contact numbers; connected parties must be identified and screened but may be *verified* risk-based; identification may draw on registries and annual reports; for legal arrangements, CDD means identifying trust relevant parties under `§6.14`.

**One gap to flag:** `§6.6` opens "For the purposes of paragraph 6.5…", but `6.5` is not exposed as a retrievable section by `get_document_section` for this document, and no `6.5` obligation appeared in `list_obligations`. The clause that actually imposes "identify the customer" is therefore not directly citable from the index as it stands — a sectioning/extraction gap rather than a gap in the Notice. Also note `list_obligations` is rule-based (`extractor: rule-based/modal-verb@1`) and returned 126 obligations across 3 pages; I read pages 1–2, which cover all of paras 6–8 plus 9–11. Page 3 (paras 11A onward: value transfers, record keeping, STRs, internal policies) is outside the CDD scope of your question.
