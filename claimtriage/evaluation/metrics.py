"""RAG quality metrics (L3, L6) - TruLens-equivalent, computed locally so they
cost nothing and are reproducible. Definitions:

groundedness       Share of rationale sentences that are supported by the cited
                   passages: a sentence is supported when >= 50% of its content
                   words (non-stopword tokens of length >= 3) occur in the union
                   of the cited clauses' text. (Proxy for TruLens groundedness /
                   statement-level NLI.)
context_relevance  Mean cosine similarity (all-MiniLM-L6-v2) between the claim
                   description and each passage returned by the agent's own
                   search_policy calls (the retrieved context; modifier listings
                   are excluded), via the retriever's /similarity endpoint.
faithfulness       Share of returned citations whose quote is found verbatim
                   (normalised, >= 90% token match) in the clause it cites AND
                   whose clause was retrieved in the run.
hallucination      A run hallucinates if ANY drafted citation (including first
                   drafts later caught by the verifier) cites a clause never
                   retrieved or quotes text not in the clause. Rate = share of
                   runs. Reported both pre-verification (model behaviour) and
                   post-verification (what the caller receives; should be 0).
"""
from __future__ import annotations

import re

from ..agents.verifier import quote_found

STOP = set("the and for that this with from have has had are was were been being not but any all its their there "
           "which when where what who will shall would should could into under upon also such than then them they "
           "these those other only each more most some may can claim policy".split())


def content_words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]{3,}", (text or "").lower()) if w not in STOP}


def groundedness(rationale: str, cited_texts: list[str]) -> float | None:
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", rationale or "") if len(content_words(s)) >= 3]
    if not sentences:
        return None
    pool = set().union(*[content_words(t) for t in cited_texts]) if cited_texts else set()
    supported = sum(1 for s in sentences if len(content_words(s) & pool) / len(content_words(s)) >= 0.5)
    return supported / len(sentences)


def faithfulness(citations: list[dict], retrieved_texts: dict[str, str]) -> float | None:
    if not citations:
        return None
    ok = sum(1 for c in citations if c.get("clause_id") in retrieved_texts
             and quote_found(c.get("quote", ""), retrieved_texts[c["clause_id"]]))
    return ok / len(citations)


def searched_texts(coverage: dict) -> list[str]:
    texts = coverage.get("retrieved_texts") or {}
    ids = list(dict.fromkeys(i for s in coverage.get("searches") or [] for i in s.get("result_ids", [])))
    return [texts[i] for i in ids if i in texts]


def drafts_hallucinated(verification_events: list[dict]) -> bool:
    for ev in verification_events:
        for f in ev.get("failures", []):
            if f.get("check") in ("V3_citation_retrieved", "V4_quote_in_clause"):
                return True
    return False


def run_metrics(run: dict, events: list[dict], relevance_scores: list[float] | None) -> dict:
    res = (run.get("result") or {})
    cov = run.get("coverage") or {}
    texts = cov.get("retrieved_texts", {})
    cits = res.get("citations", [])
    ver_events = [e for e in events if e.get("kind") == "verification"]
    post_halluc = any(c.get("clause_id") not in texts or not quote_found(c.get("quote", ""), texts[c["clause_id"]])
                      for c in cits)
    return {
        "groundedness": groundedness(res.get("rationale", ""), [texts.get(c["clause_id"], "") for c in cits]),
        "context_relevance": (sum(relevance_scores) / len(relevance_scores)) if relevance_scores else None,
        "faithfulness": faithfulness(cits, texts),
        "hallucination_pre": drafts_hallucinated(ver_events) or post_halluc,
        "hallucination_post": post_halluc,
    }
