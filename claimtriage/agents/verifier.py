"""Self-verification of a drafted coverage position (A3).

Deterministic checks run first (cheap, exact):
  V1 position is a valid enum value
  V2 a non-indeterminate position has at least one citation
  V3 every citation resolves to a clause retrieved in THIS run
  V4 every quote is found in that clause's text (normalised / near-verbatim)
  V5 a modifier search was performed (find_modifiers or a search of the
     exclusions/conditions sections) - the agent looked beyond the cover clause
  V6 decisive defined terms are resolved: if a cited clause depends on a term
     stated elsewhere (e.g. 'Limitations as to Use'), the agent must have searched
     for it AND cited the passage that gives it content (the 4.1 failure mode)
  V7 fact-triggered checks: if the facts raise a known restriction (commercial
     use, intoxication, racing) the provision governing it must be in evidence
Then an independent model check (verifier_support prompt) that the cited text
supports the rationale. An unverified answer is never returned: failures are fed
back for another search with a different strategy, or the run escalates.
"""
from __future__ import annotations

import difflib
import re

from .protocol import DECISIVE_TERMS, POSITIONS, norm

WEAK_RETRIEVAL_COSINE = 0.30

FACT_CHECKS = [
    # (entity flag, regex the governing provision must match, failure message)
    ("commercial_use_indicated", r"hire or reward|social, domestic and pleasure",
     "facts indicate commercial use but the permitted-use provision (Limitations as to Use) is not in evidence"),
    ("racing_indicated", r"racing|pace mak|speed testing",
     "facts indicate racing/speed testing but the provision restricting it is not in evidence"),
    ("intoxication_indicated", r"intoxicating liquor",
     "facts mention alcohol/drugs but the intoxication exclusion is not in evidence"),
]


def quote_found(quote: str, text: str) -> bool:
    q, t = norm(quote), norm(text)
    if not q:
        return False
    if q in t:
        return True
    qt, tt = q.split(), t.split()
    if len(qt) < 4:
        return False
    sm = difflib.SequenceMatcher(a=qt, b=tt, autojunk=False)
    matched = sum(b.size for b in sm.get_matching_blocks())
    longest = max((b.size for b in sm.get_matching_blocks()), default=0)
    return matched / len(qt) >= 0.9 and longest / len(qt) >= 0.6


def _mentions(text: str, term: str) -> bool:
    return norm(term) in norm(text)


def verify_draft(draft: dict, retrieved: dict[str, dict], searches: list[dict], modifier_calls: list[str],
                 entities: dict) -> dict:
    failures: list[dict] = []
    checks: list[dict] = []

    def check(code: str, ok: bool, msg: str = "", **extra) -> None:
        checks.append({"check": code, "ok": ok, **({"detail": msg} if msg else {}), **extra})
        if not ok:
            failures.append({"check": code, "detail": msg, **extra})

    pos = draft.get("coverage_position")
    check("V1_position_valid", pos in POSITIONS, f"invalid coverage_position {pos!r}")
    cits = [c for c in (draft.get("citations") or []) if isinstance(c, dict)]
    check("V2_has_citations", pos == "indeterminate" or bool(cits), "position given without any citation")

    cited_ok: list[str] = []
    for c in cits:
        cid, quote = str(c.get("clause_id", "")), str(c.get("quote", ""))
        if cid not in retrieved:
            check("V3_citation_retrieved", False, f"clause {cid} was cited but never retrieved in this run",
                  clause_id=cid)
            continue
        if not quote_found(quote, retrieved[cid]["text"]):
            check("V4_quote_in_clause", False, f"quote not found in clause {cid}: {quote[:120]!r}", clause_id=cid)
            continue
        cited_ok.append(cid)
    if cits and len(cited_ok) == len(cits):
        check("V3_V4_citations_resolve", True)

    searched_text = " ".join(s.get("query", "") for s in searches)
    modifier_search = bool(modifier_calls) or any(
        re.search(r"section (v|vi|vii)\b", (s.get("section") or ""), re.I) for s in searches) or \
        re.search(r"exclu|not be liable|limitation|condition", searched_text, re.I)
    check("V5_modifier_search", bool(modifier_search),
          "no modifier search: call find_modifiers on the cover clause or search the general exclusions")

    quotes = {str(c.get("clause_id")): str(c.get("quote", "")) for c in cits}
    for term in DECISIVE_TERMS:
        # only terms the cited text actually relies on (the quote), not every term the chunk mentions
        referencing = [cid for cid in cited_ok if _mentions(quotes.get(cid, ""), term)]
        if not referencing:
            continue
        looked_up = _mentions(searched_text, term)
        others = [cid for cid in cited_ok if cid not in referencing]
        if not looked_up:
            check("V6_term_resolved", False,
                  f"cited clause {referencing[0]} depends on '{term}', which is stated elsewhere; search for '{term}' "
                  "and its content (e.g. permitted purposes of use)", term=term)
        elif not others:
            check("V6_term_resolved", False,
                  f"'{term}' was searched but the passage giving it content is not cited", term=term)
        else:
            check("V6_term_resolved", True, term=term)

    evidence = " ".join(retrieved[cid]["text"] for cid in cited_ok)
    for flag, pattern, msg in FACT_CHECKS:
        if entities.get(flag):
            ok = bool(re.search(pattern, evidence, re.I))
            check(f"V7_fact_{flag}", ok, "" if ok else msg + " (cite it)")

    top = max((c.get("dense_score", 0.0) for c in retrieved.values()), default=0.0)
    weak = top < WEAK_RETRIEVAL_COSINE
    checks.append({"check": "retrieval_strength", "ok": not weak, "top_cosine": round(top, 3)})
    return {"passed": not failures, "failures": failures, "checks": checks, "weak_retrieval": weak,
            "top_cosine": round(top, 3), "cited_ok": cited_ok}
