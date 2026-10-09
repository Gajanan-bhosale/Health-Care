"""The agent's policy: base prompt + learned playbook rules + guardrail switches.

This is the only artifact the improvement loop is allowed to modify. Every
version is saved to disk with the patch that produced it, so the history of
"why does the agent do X" is auditable.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

from .tools import GUARDRAILS

ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = ROOT / "policies"


class Rule(BaseModel):
    id: str
    text: str
    added_in: int = 0
    rationale: str = ""


class Policy(BaseModel):
    version: int = 0
    base_prompt: str
    rules: list[Rule] = Field(default_factory=list)
    guardrails: dict[str, bool] = Field(default_factory=lambda: {k: False for k in GUARDRAILS})
    history: list[dict] = Field(default_factory=list)  # applied patches, newest last

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        return p

    def render_rules(self) -> str:
        if not self.rules:
            return ""
        lines = ["", "## Playbook (rules learned from evaluation)"]
        lines += [f"- [{r.id}] {r.text}" for r in self.rules]
        return "\n".join(lines)

    def summary(self) -> dict:
        return {"version": self.version, "rules": [r.id for r in self.rules],
                "guardrails_on": [k for k, v in self.guardrails.items() if v]}


def initial_policy() -> Policy:
    base = (POLICY_DIR / "base_prompt.md").read_text(encoding="utf-8")
    return Policy(version=0, base_prompt=base)


if __name__ == "__main__":
    out = initial_policy().save(POLICY_DIR / "policy_v0.json")
    print(json.dumps({"wrote": str(out)}))
