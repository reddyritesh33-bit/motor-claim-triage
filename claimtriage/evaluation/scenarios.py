"""Load eval/scenarios.yaml and build full FNOL payloads (deep-merge of base +
overrides). Shared by the evaluation runner, the Locust load test and the web
client's sample picker."""
from __future__ import annotations

import copy
import random
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[2] / "eval" / "scenarios.yaml"


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(path: str | Path = DEFAULT_PATH) -> tuple[dict, list[dict]]:
    d = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return d["base"], d["scenarios"]


def build(base: dict, scenario: dict, reference: str | None = None) -> dict:
    fnol = deep_merge(base, scenario.get("overrides", {}))
    fnol["claim_reference"] = reference or f"FNOL-{random.randint(10**9, 10**10 - 1)}"
    return fnol


def samples(path: str | Path = DEFAULT_PATH) -> list[dict]:
    base, scenarios = load(path)
    return [{"id": s["id"], "title": s["title"], "fnol": build(base, s, "FNOL-000000"), "expect": s["expect"]}
            for s in scenarios]
