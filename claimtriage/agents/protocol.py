"""Wire protocol shared by the agents, the gateway stub and the evaluation code.

Each agent step sends the model a compact JSON STATE block (rebuilt every step,
so the prompt does not grow with history) and receives one JSON action.
Untrusted text (the claimant's description, retrieved policy text) only ever
appears inside STATE as JSON string values - never as instructions.
"""
from __future__ import annotations

import json
import re

STATE_OPEN, STATE_CLOSE = "<<STATE>>", "<</STATE>>"

COVERAGE_ACTIONS = ("search_policy", "get_clause", "find_modifiers", "get_fnol_field", "final")
POSITIONS = ("likely_covered", "covered_with_conditions", "likely_not_covered", "indeterminate")

# Defined terms whose meaning decides cover and which the wording states elsewhere
# (requirement 4.1). If a cited clause relies on one of these, the agent must have
# looked it up and must cite the passage that gives it content.
DECISIVE_TERMS = ("Limitations as to Use", "Driver's Clause")


def wrap_state(state: dict) -> str:
    return f"{STATE_OPEN}\n{json.dumps(state, ensure_ascii=False, default=str)}\n{STATE_CLOSE}"


def extract_state(messages: list[dict]) -> dict:
    for m in reversed(messages):
        c = m.get("content", "")
        if STATE_OPEN in c:
            body = c.split(STATE_OPEN, 1)[1].split(STATE_CLOSE, 1)[0]
            try:
                return json.loads(body)
            except json.JSONDecodeError:
                return {}
    return {}


def parse_json_object(text: str) -> dict:
    """Parse the first JSON object in a model response (tolerates code fences)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object in model output")


def norm(s: str) -> str:
    s = (s or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"(\w)-\s+(\w)", r"\1\2", s)
    return re.sub(r"\s+", " ", s).strip().lower()
