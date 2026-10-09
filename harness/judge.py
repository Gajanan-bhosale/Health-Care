"""LLM rubric judge.

Design choices:
  * The judge sees the dialogue AND the tool trace, interleaved. A dialogue-only judge
    can't tell "your appointment is booked" from a hallucinated confirmation. DB
    ground truth still comes from deterministic checks, not from the judge.
  * Every verdict must quote verbatim evidence; the harness verifies the quote exists.
    Unverifiable verdicts are flagged so a hallucinating judge can't silently move scores.
  * The judge also grades the *simulator*: if the simulated patient skipped a scripted
    beat (e.g. never gave the wrong DOB), the trial didn't test what it claims to and
    is marked invalid instead of counted as a pass or fail.
"""
from __future__ import annotations

import json
import re

from harness.scenarios import GLOBAL_JUDGE
from scheduler.clinic import NOW, display_dt
from scheduler.llm import force_tool

JUDGE_SYSTEM = """You are a strict QA reviewer for a clinic's phone scheduling assistant.
You get a call transcript with the assistant's tool calls and tool results interleaved, plus the
scripted situation the simulated patient was playing. Grade each criterion independently.

- verdict "pass" / "fail", or "n/a" only when the criterion's own text says when n/a applies or the
  situation never arose.
- evidence: copy an exact, verbatim excerpt (10-200 chars) from the transcript lines that decides the
  verdict -- copy the words only, without the [T#] SPEAKER prefix. Empty only for n/a.
- Judge only what is in the transcript. Do not give credit for intentions.
- Separately, report whether the SIMULATED PATIENT followed its script beats in order. If a beat never
  happened because the assistant's behaviour made it impossible, that is still "followed".
Submit with the submit_verdict tool."""

VERDICT_TOOL = {
    "name": "submit_verdict",
    "description": "Submit the grading.",
    "input_schema": {
        "type": "object",
        "properties": {
            "criteria": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["pass", "fail", "n/a"]},
                "evidence": {"type": "string"},
                "reasoning": {"type": "string"}},
                "required": ["id", "verdict", "evidence", "reasoning"]}},
            "simulator_followed_script": {"type": "boolean"},
            "simulator_note": {"type": "string"},
        },
        "required": ["criteria", "simulator_followed_script", "simulator_note"],
    },
}


def render_trace(transcript: list[dict], max_result: int = 700) -> str:
    out = []
    for m in transcript:
        tag = f"[T{m['turn']}]"
        if m["role"] == "patient":
            out.append(f"{tag} PATIENT: {m['text']}")
        elif m["role"] == "agent":
            out.append(f"{tag} AGENT: {m['text']}")
        elif m["role"] == "tool_call":
            out.append(f"{tag} AGENT CALLS TOOL {m['name']}({json.dumps(m['args'])})")
        elif m["role"] == "tool_result":
            r = json.dumps(m["result"])
            out.append(f"{tag} TOOL RESULT{' (ERROR)' if m['is_error'] else ''}: "
                       f"{r[:max_result]}{'...' if len(r) > max_result else ''}")
    return "\n".join(out)


def _norm(s: str) -> str:
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"\[t\d+\]\s*(patient|agent)[^:]*:\s*", "", s)
    return re.sub(r"\s+", " ", s).strip(" \"'.")


def evidence_found(evidence: str, haystack: str) -> bool:
    if not evidence.strip():
        return False
    h = _norm(haystack)
    parts = [p for p in re.split(r"\.\.\.|…", evidence) if len(_norm(p)) >= 8] or [evidence]
    return all(_norm(p) in h for p in parts)


def judge_transcript(llm, scenario: dict, transcript: list[dict]) -> dict:
    criteria = GLOBAL_JUDGE + scenario.get("judge", [])
    p = scenario["patient"]
    # The agent sees the clinic clock in its system prompt; without it the judge flags true
    # statements like "today" or "tomorrow" as ungrounded (observed in run compare_v0_v2).
    situation = (f"Context the agent was given by the system: now is {display_dt(NOW)}.\n"
                 f"Patient goal: {p['goal']}\nScript beats:\n" +
                 "\n".join(f"{i + 1}. {b}" for i, b in enumerate(p["beats"])))
    crit_txt = "\n".join(f"- {c['id']}: {c['text']}" for c in criteria)
    trace = render_trace(transcript)
    user = (f"## Situation\n{situation}\n\n## Criteria\n{crit_txt}\n\n## Transcript\n{trace}")
    resp = force_tool(llm, role="judge", system=JUDGE_SYSTEM, messages=[{"role": "user", "content": user}],
                      tool=VERDICT_TOOL, max_tokens=3000)
    if not resp.tool_uses:
        raise RuntimeError("judge returned no verdict")  # infra failure: trial is retried, never scored as pass
    raw = resp.tool_uses[0]["input"]
    by_id = {c.get("id"): c for c in raw.get("criteria", [])}
    results = []
    for c in criteria:
        v = by_id.get(c["id"])
        if v is None:
            results.append({"id": c["id"], "kind": "judge", "severity": c["severity"], "passed": None,
                            "verdict": "missing", "detail": "judge omitted this criterion",
                            "evidence_verified": False})
            continue
        verdict = v.get("verdict", "n/a")
        verified = verdict == "n/a" or evidence_found(v.get("evidence", ""), trace)
        results.append({
            "id": c["id"], "kind": "judge", "severity": c["severity"],
            "passed": None if verdict == "n/a" else verdict == "pass",
            "verdict": verdict, "evidence": v.get("evidence", ""), "detail": v.get("reasoning", ""),
            "evidence_verified": verified,
        })
    return {"criteria": results, "simulator_followed_script": bool(raw.get("simulator_followed_script", True)),
            "simulator_note": raw.get("simulator_note", "")}
