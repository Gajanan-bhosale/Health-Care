"""Thin model-backend layer so the agent, simulator, judge and optimizer can run on
the Anthropic API or on a scripted fake (offline tests)."""
from __future__ import annotations

import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

MODELS = {
    "agent": os.environ.get("AGENT_MODEL", "claude-haiku-5-5"),
    "patient": os.environ.get("PATIENT_MODEL", "claude-haiku-5-5"),
    "judge": os.environ.get("JUDGE_MODEL", "claude-sonnet-5-5"),
    "optimizer": os.environ.get("OPTIMIZER_MODEL", "claude-opus-5-5"),
}


@dataclass
class LLMResponse:
    content: list[dict]  # Anthropic-style blocks: {"type": "text"|"tool_use", ...}
    stop_reason: str = "end_turn"

    @property
    def text(self) -> str:
        return "".join(b["text"] for b in self.content if b["type"] == "text").strip()

    @property
    def tool_uses(self) -> list[dict]:
        return [b for b in self.content if b["type"] == "tool_use"]


@dataclass
class Usage:
    lock: threading.Lock = field(default_factory=threading.Lock)
    by_role: dict = field(default_factory=lambda: defaultdict(lambda: {"calls": 0, "in": 0, "out": 0}))

    def add(self, role: str, i: int, o: int) -> None:
        with self.lock:
            r = self.by_role[role]
            r["calls"] += 1
            r["in"] += i
            r["out"] += o

    def snapshot(self) -> dict:
        with self.lock:
            return {k: dict(v) for k, v in self.by_role.items()}


USAGE = Usage()


def load_dotenv(path: Path = Path(__file__).resolve().parent.parent / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


class AnthropicLLM:
    def __init__(self):
        import anthropic
        load_dotenv()
        self.client = anthropic.Anthropic(max_retries=6, timeout=120)

    def create(self, *, role: str, system: str, messages: list[dict], tools: list[dict] | None = None,
               tool_choice: dict | None = None, max_tokens: int = 1024) -> LLMResponse:
        kw: dict[str, Any] = dict(model=MODELS[role], system=system, messages=messages, max_tokens=max_tokens)
        if tools:
            kw["tools"] = tools
        if tool_choice:
            kw["tool_choice"] = tool_choice
        resp = self.client.messages.create(**kw)
        USAGE.add(role, resp.usage.input_tokens, resp.usage.output_tokens)
        blocks = []
        for b in resp.content:
            if b.type == "text":
                blocks.append({"type": "text", "text": b.text})
            elif b.type == "tool_use":
                blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
        return LLMResponse(blocks, resp.stop_reason or "end_turn")


_NO_FORCED_TOOL: set[str] = set()  # roles whose model rejected forced tool_choice


def force_tool(llm, *, role: str, system: str, messages: list[dict], tool: dict,
               max_tokens: int = 3000) -> LLMResponse:
    """Get a structured answer via a tool call. Uses forced tool_choice where the model allows
    it; otherwise falls back to auto + an explicit instruction, and re-asks if it answers in prose."""
    msgs = list(messages)
    for _ in range(3):
        forced = role not in _NO_FORCED_TOOL
        sys = system if forced else f"{system}\n\nYou MUST respond by calling the {tool['name']} tool."
        try:
            resp = llm.create(role=role, system=sys, messages=msgs, tools=[tool], max_tokens=max_tokens,
                              tool_choice={"type": "tool", "name": tool["name"]} if forced else {"type": "auto"})
        except Exception as e:  # noqa: BLE001
            if forced and "tool_choice" in str(e):
                _NO_FORCED_TOOL.add(role)
                continue
            raise
        if any(t["name"] == tool["name"] for t in resp.tool_uses):
            return resp
        msgs = msgs + [{"role": "assistant", "content": resp.content or [{"type": "text", "text": "..."}]},
                       {"role": "user", "content": f"Please submit your answer by calling the {tool['name']} tool."}]
    return resp


class ScriptedLLM:
    """Offline backend: a function decides each response. Used by tests."""

    def __init__(self, fn: Callable[..., LLMResponse]):
        self.fn = fn

    def create(self, **kw) -> LLMResponse:
        return self.fn(**kw)


def text(t: str) -> LLMResponse:
    return LLMResponse([{"type": "text", "text": t}])


def tool_call(name: str, args: dict, id_: str = "tu_1", say: str = "") -> LLMResponse:
    blocks = [{"type": "text", "text": say}] if say else []
    return LLMResponse(blocks + [{"type": "tool_use", "id": id_, "name": name, "input": args}], "tool_use")
