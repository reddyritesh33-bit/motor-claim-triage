"""Deterministic stub "model" (load testing, offline dev, CI).

It reads the same STATE block a real model would receive and answers with
fixed rule-based JSON. It follows the same multi-hop pattern the prompts ask
for (cover -> modifiers -> resolve referenced terms -> final), so a load test
exercises the full agent loop and tool calls at realistic step counts.
It is NOT used to produce reported quality numbers for the real model.
"""
from __future__ import annotations

import json
import re

from ..agents.heuristics import rule_based_triage
from ..agents.protocol import extract_state, norm

# (cue in description, query to run, pattern the excluding text must match, position, rationale)
EXCLUSION_CUES = [
    (r"\btyres?\b.*\b(burst|torn|puncture)|no other part", "damage to tyres and tubes unless the vehicle is damaged at "
     "the same time", r"tyres and tubes", "likely_not_covered",
     "Only the tyre was damaged; Section I excludes damage to tyres and tubes unless the vehicle is damaged at the "
     "same time."),
    (r"wear and tear|engine seized|no accident|mechanical", "consequential loss depreciation wear and tear mechanical "
     "or electrical breakdown failures or breakages", r"mechanical or electrical breakdown", "likely_not_covered",
     "The loss is a mechanical failure without an insured peril; Section I excludes wear and tear and mechanical "
     "or electrical breakdown."),
    (r"nepal|bhutan|bangladesh|sri lanka|outside india|abroad", "accidental loss outside the geographical area",
     r"geographical area", "indeterminate",
     "The loss occurred outside India; General Exclusion 1 removes cover outside the geographical area, which is "
     "stated in the schedule and not in the indexed documents."),
]

COVER_QUERIES = {
    "own_damage": "loss or damage to the vehicle insured by accidental external means",
    "theft": "loss or damage to the vehicle insured by burglary housebreaking or theft",
    "third_party": "liability to third parties death bodily injury damage to property",
    "total_loss": "loss or damage to the vehicle insured by fire explosion self ignition or lightning flood",
}


def _sentence(text: str, pattern: str) -> str | None:
    """Return a verbatim span of `text` around the first match of `pattern`."""
    m = re.search(pattern, text, re.I)
    if not m:
        line = next((ln for ln in text.splitlines() if len(ln.split()) >= 6), "")
        return line.strip() or None
    start = text.rfind("\n", 0, m.start()) + 1
    end = text.find("\n", m.end())
    end = len(text) if end == -1 else end
    nxt = text.find("\n", end + 1)
    if end - start < 90 and nxt != -1:
        end = nxt
    return text[start:end].strip()


def _find(retrieved: list[dict], pattern: str, exclude: set[str] = frozenset()) -> dict | None:
    for c in retrieved:
        if c["clause_id"] not in exclude and re.search(pattern, c["text"], re.I):
            return c
    return None


def _triage(state: dict) -> dict:
    return rule_based_triage(state.get("fnol", {}), state.get("derived", {}))


def _coverage(state: dict) -> dict:
    claim = state.get("claim", {})
    ent = claim.get("entities", {})
    retrieved: list[dict] = state.get("retrieved", [])
    searched = [norm(q) for q in state.get("searched_queries", [])]
    mod_calls = state.get("modifier_calls", [])
    ctype = claim.get("claim_type", "own_damage")
    desc = claim.get("description", "")
    commercial = ent.get("commercial_use_indicated") or ent.get("racing_indicated")

    if not searched:
        return {"thought": "Find the cover clause first.", "action": "search_policy",
                "args": {"query": COVER_QUERIES.get(ctype, COVER_QUERIES["own_damage"])}}
    cover = _find(retrieved, r"will indemnify|loss or damage to the vehicle insured|liability to third part") \
        or (retrieved[0] if retrieved else None)
    if cover and not mod_calls:
        return {"thought": "Check which provisions modify the cover clause.", "action": "find_modifiers",
                "args": {"clause_id": cover["clause_id"]}}
    ge = _find(retrieved, r"Limitations as to Use")
    if commercial and not ge and not any("limitations as to use" in q for q in searched):
        return {"thought": "Use of the vehicle may matter; find the general exclusion on use.", "action": "search_policy",
                "args": {"query": "general exclusions being used otherwise than in accordance with Limitations as to Use"}}
    if commercial and not any("hire or reward" in q for q in searched):
        return {"thought": "The exclusion refers to 'Limitations as to Use', defined elsewhere; search its content.",
                "action": "search_policy",
                "args": {"query": "Limitations as to Use: vehicle used for social domestic pleasure purpose excluding "
                                  "use for hire or reward carriage of goods"}}
    cue = next((c for c in EXCLUSION_CUES if re.search(c[0], desc, re.I)), None)
    if cue and not any(norm(cue[1]) == q for q in searched):
        return {"thought": "The facts suggest an exclusion may apply; search for it.", "action": "search_policy",
                "args": {"query": cue[1]}}
    if ent.get("intoxication_indicated") and not any("intoxicating" in q for q in searched):
        return {"thought": "Check the intoxication exclusion.", "action": "search_policy",
                "args": {"query": "not liable whilst under the influence of intoxicating liquor or drugs"}}
    if ent.get("late_intimation") and not any("intimate" in q for q in searched):
        return {"thought": "Intimation was late; check claim procedure timelines.", "action": "search_policy",
                "args": {"query": "insured should intimate the Company within 15 days OD and 24 hrs theft"}}
    if ent.get("proximity_case") and not any("proximity" in q for q in searched):
        return {"thought": "Loss is soon after inception; check proximity rule.", "action": "search_policy",
                "args": {"query": "Proximity Check date of loss within 30 days of commencement of cover"}}

    # ---------------- final
    cites, mods, position = [], [c["clause_id"] for c in retrieved if re.search(r"Section V", c.get("section", ""))], \
        "likely_covered"
    rationale = []
    if cover:
        peril = {"theft": r"burglary housebreaking or theft", "total_loss": r"fire explosion self.?ignition",
                 "third_party": r"bodily injury|liable to pay"}.get(ctype, r"accidental external means")
        for pat in (r"flood|inundation", r"earthquake", r"riot and strike", r"malicious act", r"fire explosion"):
            if re.search(pat, desc + " " + ("fire" if ctype == "total_loss" else ""), re.I) and                     re.search(pat, cover["text"], re.I):
                peril = pat
                break
        q = _sentence(cover["text"], peril + r"|will indemnify")
        if q:
            cites.append({"clause_id": cover["clause_id"], "quote": q})
    if commercial:
        use = _find(retrieved, r"hire or reward", exclude={cover["clause_id"]} if cover else set())
        use = _find(retrieved, r"social, domestic and pleasure") or use
        if ge:
            cites.append({"clause_id": ge["clause_id"], "quote": _sentence(ge["text"], r"Limitations as to Use")})
        if use:
            cites.append({"clause_id": use["clause_id"], "quote": _sentence(use["text"], r"hire or reward")})
            position = "likely_not_covered"
            rationale.append("The vehicle was being used commercially at the time of loss. General Exclusion 3(a) "
                             "removes cover when the vehicle is used otherwise than in accordance with the "
                             "Limitations as to Use, and the permitted use excludes hire or reward and carriage "
                             "of goods.")
        else:
            position = "indeterminate"
            rationale.append("Commercial use is indicated but the permitted-use provision was not found.")
    if ent.get("intoxication_indicated"):
        ix = _find(retrieved, r"intoxicating liquor")
        if ix:
            cites.append({"clause_id": ix["clause_id"], "quote": _sentence(ix["text"], r"intoxicating liquor")})
            position = "likely_not_covered"
            rationale.append("The description indicates the driver had consumed alcohol; Section I excludes loss "
                             "while the driver is under the influence of intoxicating liquor.")
    if cue:
        ex = _find(retrieved, cue[2])
        if ex:
            cites.append({"clause_id": ex["clause_id"], "quote": _sentence(ex["text"], cue[2])})
            position = cue[3]
            rationale.append(cue[4])
    if position == "likely_covered":
        q0 = cites[0]["quote"].rstrip(" ;,.") if cites else "the cover clause"
        rationale.append(f"The policy covers loss or damage as stated: '{q0}'. On the stated facts no retrieved "
                         "exclusion, condition or limitation of use applies to this loss.")
        if ent.get("late_intimation") or ent.get("proximity_case"):
            position = "covered_with_conditions"
            cp = _find(retrieved, r"within 15 days|Proximity Check")
            if cp:
                cites.append({"clause_id": cp["clause_id"],
                              "quote": _sentence(cp["text"], r"within 15 days|Proximity Check")})
            rationale.append("Admissibility is subject to the claim-procedure conditions noted.")
    if state.get("fault") == "bad_citation" and not state.get("feedback"):
        cites.append({"clause_id": cover["clause_id"] if cover else "PW-S1-001",
                      "quote": "The Company will pay all delivery-related accidents in full without any deductible."})
    cites = [c for c in cites if c.get("quote")]
    return {"thought": "Evidence gathered; draft the position.", "action": "final",
            "args": {"coverage_position": position, "rationale": " ".join(rationale), "citations": cites,
                     "modifiers_considered": mods[:6], "conflicts": [], "confidence": 0.7}}


def _verify(state: dict) -> dict:
    return {"supported": True, "unsupported_claims": [], "notes": "stub verifier"}


def stub_complete(task: str, messages: list[dict]) -> str:
    state = extract_state(messages)
    if task == "triage_extract":
        out = _triage(state)
    elif task == "coverage_step":
        out = _coverage(state)
    elif task == "verify_support":
        out = _verify(state)
    else:
        out = {"error": f"stub has no response for task {task}"}
    return json.dumps(out)
