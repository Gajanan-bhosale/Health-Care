"""The scheduling agent: a manual tool-use loop over the Messages API.

State management:
  * `messages` is the full Anthropic-format history (text + tool_use + tool_result),
    so the model always sees exactly what its tools returned.
  * `SessionState` is the authoritative, code-owned state (who is verified, lockout,
    escalations). It is re-rendered into the system prompt every model call, so the
    model never has to re-derive facts like "is this caller verified" from history.
"""
from __future__ import annotations

import json

from .clinic import Clinic
from .llm import LLMResponse
from .policy import Policy
from .tools import TOOL_SCHEMAS, SessionState, ToolExecutor, red_flags

MAX_TOOL_STEPS = 8
FALLBACK = ("I'm sorry, I'm having trouble completing that right now. "
            "Let me have a member of our staff call you back.")


class SchedulingAgent:
    def __init__(self, llm, policy: Policy, clinic: Clinic):
        self.llm = llm
        self.policy = policy
        self.session = SessionState()
        self.tools = ToolExecutor(clinic, self.session, policy.guardrails)
        self.messages: list[dict] = []
        self.turn = 0
        self.alerts: list[str] = []

    def system_prompt(self) -> str:
        parts = [self.policy.base_prompt.strip(), self.policy.render_rules(),
                 "", "## Session state (maintained by the system; authoritative)", self.session.render()]
        if self.alerts:
            parts += ["", "## SAFETY ALERT", *self.alerts]
        return "\n".join(parts)

    def respond(self, patient_text: str) -> str:
        self.turn += 1
        if self.policy.guardrails.get("emergency_screen"):
            hits = red_flags(patient_text)
            if hits:
                self.alerts.append(
                    f"Turn {self.turn}: the caller's message matched red-flag symptom patterns ({', '.join(hits)}). "
                    "If this could be a current emergency, stop scheduling, tell them to call 911 now, and call "
                    "escalate_to_human with urgency 'emergency'.")
        self.messages.append({"role": "user", "content": patient_text})

        for _ in range(MAX_TOOL_STEPS):
            resp: LLMResponse = self.llm.create(role="agent", system=self.system_prompt(),
                                                messages=self.messages, tools=TOOL_SCHEMAS)
            content = resp.content or [{"type": "text", "text": "..."}]
            self.messages.append({"role": "assistant", "content": content})
            if not resp.tool_uses:
                return resp.text or "..."
            results = []
            for tu in resp.tool_uses:
                out, is_error = self.tools.execute(tu["name"], tu.get("input") or {}, self.turn)
                results.append({"type": "tool_result", "tool_use_id": tu["id"],
                                "content": json.dumps(out), "is_error": is_error})
            self.messages.append({"role": "user", "content": results})

        # Ran out of tool steps: close the turn with a safe message rather than looping forever.
        self.messages.append({"role": "assistant", "content": [{"type": "text", "text": FALLBACK}]})
        return FALLBACK
