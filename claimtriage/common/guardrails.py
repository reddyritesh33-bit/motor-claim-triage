"""Guardrails (L4).

1. Prompt-injection detection on every free-text field that can reach a model.
   Heuristic, layered: instruction-override phrases, role/turn markers, tool or
   system references, attempts to dictate the determination, and encoded payloads.
   A hit quarantines the FNOL at the boundary - injected text never reaches an
   agent that holds tools. Free text that passes is still wrapped as untrusted
   data inside prompts (defence in depth).
2. Toxicity detection on input and output (lexicon + severity weighting; no
   network dependency). Toxic input is masked before it reaches a model and the
   run is flagged for a human; toxic output is blocked.
3. Output filtering: the system may not issue a determination it is not
   authorised to make (approve / reject / settle / pay amounts).
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

INJECTION_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("override", re.compile(r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|above|all|earlier|system|your)\b[^.\n]{0,30}\b(instruction|prompt|rule|direction|guideline)s?", re.I)),
    ("new_instructions", re.compile(r"\b(new|updated|real)\s+(instructions?|system\s+prompt|rules)\s*[:\-]", re.I)),
    ("role_marker", re.compile(r"(^|\n)\s*(system|assistant|developer|tool)\s*:", re.I)),
    ("chat_template", re.compile(r"<\|?(im_start|im_end|system|endoftext)\|?>|\[/?INST\]|<</?SYS>>", re.I)),
    ("persona", re.compile(r"\byou\s+are\s+(now|no\s+longer)\b|\bact\s+as\s+(an?\s+)?(admin|developer|system)|\bjailbreak\b|\bDAN\b mode", re.I)),
    ("dictate_outcome", re.compile(r"\b(mark|set|classify|route|flag)\b[^.\n]{0,30}\b(claim|this)\b[^.\n]{0,30}\b(approved|covered|fast[- ]?track|accepted|paid)\b", re.I)),
    ("dictate_outcome2", re.compile(r"\b(approve|pay out|settle)\s+(this|my|the)\s+claim\s+(immediately|now|without)", re.I)),
    ("tool_abuse", re.compile(r"\b(call|invoke|use|run)\s+(the\s+)?(tool|function|search_policy|get_clause|find_modifiers|get_fnol_field)\b", re.I)),
    ("prompt_leak", re.compile(r"\b(reveal|print|show|repeat)\b[^.\n]{0,20}\b(system prompt|your instructions|hidden prompt)\b", re.I)),
]

_B64 = re.compile(r"[A-Za-z0-9+/]{40,}={0,2}")

# Lexicon kept deliberately small; severity 2 = slur/threat, 1 = profanity.
TOXIC_LEXICON: dict[str, int] = {
    "idiot": 1, "stupid": 1, "bastard": 2, "bloody fool": 1, "shit": 1, "fuck": 2, "fucking": 2,
    "asshole": 2, "moron": 1, "scum": 2, "kill you": 2, "i will kill": 2, "burn your office": 2,
    "chutiya": 2, "harami": 2, "kamina": 1, "saala": 1, "bhenchod": 2, "madarchod": 2,
}
_TOXIC_RE = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(TOXIC_LEXICON, key=len, reverse=True)) + r")\b", re.I)

FORBIDDEN_DETERMINATIONS: list[tuple[str, re.Pattern]] = [
    ("approve_reject", re.compile(r"\b(claim|it)\s+(is|has\s+been|will\s+be|stands)\s+(hereby\s+)?(approved|rejected|denied|repudiated|accepted|settled|sanctioned)\b", re.I)),
    ("we_approve", re.compile(r"\bwe\s+(hereby\s+)?(approve|reject|deny|repudiate|sanction)\b", re.I)),
    ("payment_promise", re.compile(r"\b(we|the company|insurer)\s+(will|shall)\s+(pay|reimburse|settle)\b", re.I)),
    ("settlement_amount", re.compile(r"\b(settle(ment)?|payable|pay(out)?)\b[^.\n]{0,30}(rs\.?|inr|₹)\s?[0-9][0-9,]*", re.I)),
]


@dataclass
class GuardResult:
    ok: bool
    findings: list[dict] = field(default_factory=list)


def detect_injection(fields: dict[str, str]) -> GuardResult:
    findings = []
    for loc, text in fields.items():
        if not text:
            continue
        for name, pat in INJECTION_PATTERNS:
            m = pat.search(text)
            if m:
                findings.append({"loc": loc, "rule": name, "match": m.group(0)[:80]})
        for blob in _B64.findall(text):
            try:
                decoded = base64.b64decode(blob + "=" * (-len(blob) % 4)).decode("utf-8", "ignore")
            except Exception:
                continue
            if any(p.search(decoded) for _, p in INJECTION_PATTERNS):
                findings.append({"loc": loc, "rule": "encoded_payload", "match": blob[:40]})
    return GuardResult(ok=not findings, findings=findings)


def toxicity_score(text: str) -> tuple[float, list[str]]:
    """Score in [0,1]: weighted lexicon hits normalised by length (min 25 words)."""
    hits = [m.group(0).lower() for m in _TOXIC_RE.finditer(text or "")]
    if not hits:
        return 0.0, []
    weight = sum(TOXIC_LEXICON.get(h, 1) for h in hits)
    words = max(25, len((text or "").split()))
    return min(1.0, weight * 6 / words), hits


def mask_toxic(text: str) -> str:
    return _TOXIC_RE.sub(lambda m: "[redacted]", text or "")


TOXICITY_THRESHOLD = 0.2


def filter_output(text: str) -> tuple[str, list[str]]:
    """Remove sentences that issue a determination the system is not authorised
    to make. Returns (filtered_text, rules_triggered)."""
    triggered: list[str] = []
    sentences = re.split(r"(?<=[.!?])\s+", text or "")
    kept = []
    for s in sentences:
        hit = next((name for name, pat in FORBIDDEN_DETERMINATIONS if pat.search(s)), None)
        if hit:
            triggered.append(hit)
            kept.append("[removed: the system is not authorised to make this determination]")
        else:
            kept.append(s)
    return " ".join(kept).strip(), triggered


def free_text_fields(fnol: dict) -> dict[str, str]:
    """Every free-text field of an FNOL that can reach a model."""
    inc = fnol.get("incident", {})
    return {
        "incident.description": inc.get("description", ""),
        "incident.location.city": inc.get("location", {}).get("city", ""),
        "incident.location.state": inc.get("location", {}).get("state", ""),
        "incident.fir_number": inc.get("fir_number") or "",
        "vehicle.make": fnol.get("vehicle", {}).get("make", ""),
        "vehicle.model": fnol.get("vehicle", {}).get("model", ""),
        "driver.name": fnol.get("driver", {}).get("name", ""),
        "claimant.name": fnol.get("claimant", {}).get("name", ""),
        "policy.addons": " ".join(fnol.get("policy", {}).get("addons", []) or []),
    }
