"""Prompts are versioned artifacts (L2): prompts/<id>/<version>.yaml. Every LLM
call records prompt id, version and content hash in the run trace."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

PROMPT_DIR = Path(__file__).resolve().parents[2] / "prompts"


@dataclass(frozen=True)
class Prompt:
    id: str
    version: str
    system: str
    user: str
    max_tokens: int
    sha: str

    def render(self, state_block: str) -> list[dict]:
        return [{"role": "system", "content": self.system},
                {"role": "user", "content": self.user.replace("{state}", state_block)}]

    @property
    def ref(self) -> dict:
        return {"prompt_id": self.id, "prompt_version": self.version, "prompt_sha": self.sha}


@lru_cache
def load_prompt(prompt_id: str, version: str) -> Prompt:
    path = PROMPT_DIR / prompt_id / f"{version}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"prompt {prompt_id}@{version} not found at {path}")
    raw = path.read_text(encoding="utf-8")
    d = yaml.safe_load(raw)
    return Prompt(id=d["id"], version=d["version"], system=d["system"], user=d["user"],
                  max_tokens=int(d.get("max_tokens", 600)), sha=hashlib.sha256(raw.encode()).hexdigest()[:12])


def list_versions(prompt_id: str) -> list[str]:
    return sorted(p.stem for p in (PROMPT_DIR / prompt_id).glob("*.yaml"))
