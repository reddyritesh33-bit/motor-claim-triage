"""Rule-based heuristics shared by the stub model and the degraded (no-LLM)
fallback path. Deliberately simple and transparent."""
from __future__ import annotations

import re

COMMERCIAL = re.compile(r"\b(food )?deliver(y|ies|ing)|zomato|swiggy|uber|ola\b|rapido|ride[- ]?hail|taxi|cab service|"
                        r"for (a )?fare|paying passengers?|courier|carrying goods|goods for|rent(ed|ing)? (it )?out|"
                        r"on hire|for hire|self[- ]drive rental|transport(ing)? goods", re.I)
INTOX = re.compile(r"\b(drunk|alcohol|liquor|beer|whisky|intoxicat\w*|under the influence)\b", re.I)
THEFT = re.compile(r"\b(stolen|theft|stole|missing from|burglar\w*|break[- ]in)\b", re.I)
TOTAL = re.compile(r"\b(total loss|completely (burnt|burned|destroyed|gutted)|written off|beyond repair|fully submerged)\b", re.I)
NON_MOTOR = re.compile(r"\b(dengue|malaria|typhoid|hospitali[sz]ed for|health (policy|insurance)|family floater|mediclaim)\b", re.I)
RACING = re.compile(r"\b(race|racing|speed test|rally|track day)\b", re.I)


def rule_based_triage(f: dict, derived: dict) -> dict:
    """Keyword rules: the stub model's answer, and the degraded-path classifier
    used when the LLM is unavailable."""
    inc = f.get("incident", {})
    d = inc.get("description", "")
    if TOTAL.search(d) or (derived.get("estimate_to_idv_ratio") or 0) >= 0.75:
        ctype = "total_loss"
    elif THEFT.search(d):
        ctype = "theft"
    elif inc.get("third_party_involved") and re.search(r"\b(hit|injur|pedestrian|his car|their car|other car|"
                                                         r"another vehicle|scooter)\b", d, re.I) \
            and not re.search(r"\bmy car\b.*\b(dent|damage)", d, re.I):
        ctype = "third_party"
    else:
        ctype = "own_damage"
    in_scope = (f.get("policy", {}).get("vehicle_class") == "private_car" and derived.get("product_in_corpus", True)
                and not NON_MOTOR.search(d))
    return {
        "claim_type": ctype,
        "secondary_types": ["third_party"] if inc.get("third_party_involved") and ctype != "third_party" else [],
        "entities": {
            "cause_of_loss": (THEFT.search(d) and "theft") or (TOTAL.search(d) and "major damage") or "accident",
            "vehicle_use_at_loss": ("commercial" if COMMERCIAL.search(d) else "racing" if RACING.search(d)
                                    else "private"),
            "commercial_use_indicated": bool(COMMERCIAL.search(d)),
            "intoxication_indicated": bool(INTOX.search(d)),
            "racing_indicated": bool(RACING.search(d)),
            "third_party_injury": inc.get("injuries") in ("serious", "fatal") and inc.get("third_party_involved"),
        },
        "research_question": f"Is this {ctype.replace('_', ' ')} claim within cover, and does any exclusion, "
                             "condition or limitation of use modify that cover on these facts?",
        "in_scope": bool(in_scope),
        "out_of_scope_reason": "" if in_scope else "vehicle class or product is outside the indexed policy",
    }
