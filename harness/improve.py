"""Close the loop: failures -> structured patch -> re-run everything -> gate -> keep or reject.

  1. Run the full suite with the current policy (baseline).
  2. Package TRAIN failures (failed checks + evidence + the trace) for the optimizer model.
     Holdout scenarios are never shown to it; they only vote in the gate.
  3. The optimizer returns a typed Patch: add/edit/remove playbook rules, or flip a code
     guardrail from a fixed menu. It cannot rewrite the base prompt or invent new tools.
  4. Lint the patch: schema, length, unknown guardrails, and scenario-specific tokens
     (patient names, DOBs, appointment ids) that would mean it is overfitting.
  5. Apply -> new policy version -> re-run ALL scenarios (train + holdout).
  6. Gate: accept only if the suite improved AND no previously fully-passing scenario
     regressed. Suspected regressions are re-run once to separate flakes from real ones.
  7. Repeat until train is clean or the iteration budget runs out; write a report.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from harness.judge import render_trace
from harness.report import write_report
from harness.runner import ROOT, run_suite
from harness.scenarios import SCENARIOS_BY_ID, scenario_specific_tokens
from scheduler.llm import force_tool
from scheduler.policy import Policy, Rule
from scheduler.tools import GUARDRAILS

MAX_RULE_WORDS = 70
MAX_CHANGES = 3


class Diagnosis(BaseModel):
    scenario: str
    failed_check: str
    root_cause: str
    suspected_harness_issue: bool = False  # mislabeled check / simulator problem: flag for a human, don't patch


class Change(BaseModel):
    op: Literal["add_rule", "edit_rule", "remove_rule", "enable_guardrail", "disable_guardrail"]
    rule_id: str | None = None
    text: str | None = None
    guardrail: str | None = None
    addresses: list[str] = Field(default_factory=list)
    rationale: str


class Patch(BaseModel):
    diagnosis: list[Diagnosis]
    changes: list[Change] = Field(min_length=1, max_length=MAX_CHANGES)


def _inline_refs(schema: dict) -> dict:
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(defs[node["$ref"].split("/")[-1]])
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node
    return walk(schema)


PATCH_TOOL = {"name": "submit_patch", "description": "Submit the policy patch.",
              "input_schema": _inline_refs(Patch.model_json_schema())}

OPTIMIZER_SYSTEM = f"""You improve the policy of a clinic phone-scheduling agent using evaluation failures.

The policy has three parts:
  1. A base prompt (fixed; you cannot edit it).
  2. A playbook of short rules appended to the prompt. You may add, edit or remove rules.
  3. Code guardrails from a fixed menu that you may enable or disable. They are reliable
     but have costs; prefer them when a prompt rule cannot make behaviour reliable (e.g.
     tool flakiness, or state invariants), and prefer rules for conversational behaviour.

Process: for each failure, find the root cause in the agent's behaviour (not the symptom), then
propose the smallest set of changes (1-{MAX_CHANGES}) that fixes the root causes.

Prioritise: fix critical failures first, then major, then minor. Fewer changes are better: a single bad change
gets the whole patch rejected, and unaddressed failures will come back next iteration.

The harness can be wrong too. If the trace shows the agent behaved reasonably and the check or the
simulated patient looks mistaken, set suspected_harness_issue=true in the diagnosis and do NOT write
a rule for it. Never invent clinic policies that no tool result shows.

Hard constraints (patches violating them are rejected automatically):
- Rules must describe GENERAL behaviour that applies to any patient. Never mention specific
  patient names, dates of birth, appointment ids or codes from the transcripts.
- Each rule is at most {MAX_RULE_WORDS} words, imperative, and testable. rule_id is short snake_case.
- Do not weaken safety, privacy or verification behaviour to make a scenario pass.
- Don't touch rules that are unrelated to the failures; other scenarios currently pass with them.
- `addresses` lists the "scenario:check" failures each change targets.
Submit via the submit_patch tool."""


def failure_packets(agg: dict, out_dir: Path, max_trace_chars: int = 5000) -> list[dict]:
    packets = []
    for sid, s in agg["scenarios"].items():
        # Include passing-but-imperfect scenarios too, otherwise minor failures can never be learned.
        if s["split"] != "train" or s["score"] == 1.0:
            continue
        # Representative trial: the lowest-scoring valid one.
        trials = [json.loads(p.read_text(encoding="utf-8")) for p in out_dir.glob(f"{sid}__t*.json")]
        trials = [t for t in trials if "transcript" in t] or trials
        worst = min(trials, key=lambda t: (t.get("valid", False) is False, t["score"]))
        failed = [{k: r.get(k) for k in ("id", "kind", "severity", "detail", "evidence")}
                  for r in worst.get("results", []) if r["passed"] is False]
        if not failed:
            continue
        trace = render_trace(worst.get("transcript", []))
        sc = SCENARIOS_BY_ID[sid]
        packets.append({"scenario": sid, "tags": sc["tags"], "patient_goal": sc["patient"]["goal"],
                        "pass_rate": s["pass_rate"], "failed_checks": failed,
                        "trace": trace if len(trace) <= max_trace_chars else trace[:max_trace_chars] + "\n...[truncated]"})
    return packets


def _trial(run_dir: Path, sid: str) -> dict:
    p = sorted(run_dir.glob(f"{sid}__t*.json"))
    return json.loads(p[0].read_text(encoding="utf-8")) if p else {}


def lint_patch(patch: Patch, policy: Policy) -> list[str]:
    errs = []
    toks = scenario_specific_tokens()
    ids = {r.id for r in policy.rules}
    for i, c in enumerate(patch.changes):
        where = f"change[{i}] ({c.op})"
        if c.op in ("add_rule", "edit_rule"):
            if not c.rule_id or not c.text:
                errs.append(f"{where}: needs rule_id and text")
                continue
            if len(c.text.split()) > MAX_RULE_WORDS:
                errs.append(f"{where}: rule is {len(c.text.split())} words (max {MAX_RULE_WORDS})")
            leaked = sorted(t for t in toks if t.lower() in c.text.lower())
            if leaked:
                errs.append(f"{where}: mentions scenario-specific tokens {leaked}; rules must be general")
            if c.op == "add_rule" and c.rule_id in ids:
                errs.append(f"{where}: rule_id {c.rule_id} already exists (use edit_rule)")
            if c.op == "edit_rule" and c.rule_id not in ids:
                errs.append(f"{where}: no rule {c.rule_id} to edit")
        elif c.op == "remove_rule":
            if c.rule_id not in ids:
                errs.append(f"{where}: no rule {c.rule_id} to remove")
        else:
            if c.guardrail not in GUARDRAILS:
                errs.append(f"{where}: unknown guardrail {c.guardrail}; options {list(GUARDRAILS)}")
    return errs


def apply_patch(policy: Policy, patch: Patch, version: int | None = None) -> Policy:
    new = policy.model_copy(deep=True)
    new.version = version if version is not None else policy.version + 1
    for c in patch.changes:
        if c.op == "add_rule":
            new.rules.append(Rule(id=c.rule_id, text=c.text, added_in=new.version, rationale=c.rationale))
        elif c.op == "edit_rule":
            r = next(r for r in new.rules if r.id == c.rule_id)
            r.text, r.rationale, r.added_in = c.text, c.rationale, new.version
        elif c.op == "remove_rule":
            new.rules = [r for r in new.rules if r.id != c.rule_id]
        elif c.op == "enable_guardrail":
            new.guardrails[c.guardrail] = True
        elif c.op == "disable_guardrail":
            new.guardrails[c.guardrail] = False
    new.history.append({"version": new.version, "parent": policy.version, "patch": patch.model_dump()})
    return new


def propose_patch(llm, policy: Policy, packets: list[dict], rejected: list[dict]) -> tuple[Patch | None, list[str]]:
    guard_menu = "\n".join(f"- {k} [{'ON' if policy.guardrails.get(k) else 'off'}]: {v}" for k, v in GUARDRAILS.items())
    rules = "\n".join(f"- [{r.id}] {r.text}" for r in policy.rules) or "(none yet)"
    rej = "\n".join(f"- {json.dumps(r['patch']['changes'])}\n  rejected because: {r['reason']}" for r in rejected) or "(none)"
    body = (f"## Base prompt (read-only)\n{policy.base_prompt}\n\n## Current playbook rules\n{rules}\n\n"
            f"## Guardrail menu\n{guard_menu}\n\n## Previously rejected patches (do not repeat)\n{rej}\n\n"
            f"## Failures from the latest run ({len(packets)} scenarios)\n" +
            "\n\n".join(f"### {p['scenario']} (tags: {', '.join(p['tags'])}; pass rate {p['pass_rate']})\n"
                        f"Patient goal: {p['patient_goal']}\nFailed checks:\n{json.dumps(p['failed_checks'], indent=1)}\n"
                        f"Trace:\n{p['trace']}" for p in packets))
    messages = [{"role": "user", "content": body}]
    lint_errors: list[str] = []
    for _ in range(3):
        resp = force_tool(llm, role="optimizer", system=OPTIMIZER_SYSTEM, messages=messages, tool=PATCH_TOOL,
                          max_tokens=4000)
        if not resp.tool_uses:
            return None, ["optimizer did not call submit_patch"]
        tu = resp.tool_uses[0]
        try:
            patch = Patch.model_validate(tu["input"])
            lint_errors = lint_patch(patch, policy)
        except ValidationError as e:
            patch, lint_errors = None, [str(e)[:1500]]
        if not lint_errors:
            return patch, []
        messages += [{"role": "assistant", "content": resp.content},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tu["id"], "is_error": True,
                                                   "content": "Patch rejected by linter:\n- " + "\n- ".join(lint_errors)
                                                              + "\nFix these and submit again."}]}]
    return None, lint_errors


def gate(prev: dict, cand: dict, recheck) -> dict:
    regressed = [sid for sid, s in prev["scenarios"].items()
                 if s["pass_rate"] == 1.0 and cand["scenarios"][sid]["pass_rate"] < 1.0]
    flaky, confirmed = [], []
    if regressed:
        re_agg = recheck(regressed)
        for sid in regressed:
            (flaky if re_agg["scenarios"][sid]["pass_rate"] == 1.0 else confirmed).append(sid)
    ps, cs = prev["summary"]["all"], cand["summary"]["all"]
    improved = cs["fully_passing"] > ps["fully_passing"] or cs["mean_score"] > ps["mean_score"] + 0.005
    accept = improved and not confirmed
    reason = ("accepted" if accept else
              f"regressed {confirmed}" if confirmed else "no improvement in suite score or passing scenarios")
    return {"accept": accept, "reason": reason, "regressed": confirmed, "flaky_rechecked": flaky,
            "prev": {"all": ps, "train": prev["summary"].get("train"), "holdout": prev["summary"].get("holdout")},
            "cand": {"all": cs, "train": cand["summary"].get("train"), "holdout": cand["summary"].get("holdout")}}


def improve(llm, policy: Policy, run_dir: Path, iterations: int = 3, trials: int = 1,
            confirm_trials: int = 0, log=print, scenario_ids: list[str] | None = None) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    policy.save(run_dir / f"policy_v{policy.version}.json")
    log(f"[baseline] running suite with policy v{policy.version}")
    best_dir = run_dir / f"iter0_v{policy.version}"
    best = run_suite(llm, policy, best_dir, trials, scenario_ids)
    baseline = best
    log(f"[baseline] {best['summary']}")
    record = {"baseline": {"policy": policy.summary(), "result": best, "dir": best_dir.name},
              "iterations": [], "rejected": []}

    next_version = policy.version + 1
    for it in range(1, iterations + 1):
        packets = failure_packets(best, best_dir)
        if not packets:
            log("[loop] no train failures left; stopping")
            break
        log(f"[iter {it}] {len(packets)} failing train scenarios -> asking optimizer for a patch")
        patch, lint_errors = propose_patch(llm, policy, packets, record["rejected"])
        if patch is None:
            log(f"[iter {it}] optimizer could not produce a lint-clean patch: {lint_errors}")
            record["iterations"].append({"iteration": it, "status": "lint_failed", "errors": lint_errors})
            continue
        cand = apply_patch(policy, patch, next_version)
        cand_dir = run_dir / f"iter{it}_v{cand.version}"
        log(f"[iter {it}] candidate v{cand.version}: {[c.op + ':' + (c.rule_id or c.guardrail or '') for c in patch.changes]}")
        cand_res = run_suite(llm, cand, cand_dir, trials, scenario_ids)
        log(f"[iter {it}] candidate {cand_res['summary']}")
        decision = gate(best, cand_res,
                        lambda ids: run_suite(llm, cand, cand_dir / "recheck", trials, ids))
        log(f"[iter {it}] gate: {decision['reason']}")
        entry = {"iteration": it, "candidate_version": cand.version, "patch": patch.model_dump(),
                 "decision": decision, "result": cand_res, "dir": cand_dir.name}
        record["iterations"].append(entry)
        cand.save(run_dir / f"policy_v{cand.version}.json")
        if decision["accept"]:
            policy, best, best_dir = cand, cand_res, cand_dir
        else:
            # Tell the optimizer *why* each regressed scenario failed, not just that it did.
            why = decision["reason"]
            for sid in decision["regressed"]:
                fails = [f"{r['id']}: {r.get('detail', '')[:300]}"
                         for r in _trial(cand_dir, sid).get("results", []) if r["passed"] is False]
                why += f"\n    regressed {sid}: " + "; ".join(fails)
            record["rejected"].append({"patch": patch.model_dump(), "reason": why})
        next_version = cand.version + 1  # version numbers stay unique even for rejected candidates

    record["final"] = {"policy": policy.summary(), "result": best, "dir": best_dir.name}
    policy.save(run_dir / "policy_final.json")

    if confirm_trials:
        # Fresh, independent re-run of baseline vs final: the accepted run was selected for being
        # good, so its score is optimistically biased. This removes that selection bias.
        log(f"[confirm] fresh re-run of v0 and final with {confirm_trials} trials each")
        v0 = Policy.load(run_dir / f"policy_v{record['baseline']['policy']['version']}.json")
        record["confirm"] = {
            "baseline": run_suite(llm, v0, run_dir / "confirm_baseline", confirm_trials, scenario_ids),
            "final": run_suite(llm, policy, run_dir / "confirm_final", confirm_trials, scenario_ids),
        }
    record["baseline_result"] = baseline
    (run_dir / "loop.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
    write_report(run_dir, record, policy)
    return record


def main() -> None:
    from scheduler.llm import USAGE, AnthropicLLM
    ap = argparse.ArgumentParser(description="Run the self-improvement loop.")
    ap.add_argument("--policy", default=str(ROOT / "policies" / "policy_v0.json"))
    ap.add_argument("--iterations", type=int, default=3)
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--confirm-trials", type=int, default=0)
    ap.add_argument("--scenarios", help="comma-separated subset (e.g. for a quick demo); default: all")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    run_dir = Path(a.out or ROOT / "runs" / f"loop_{time.strftime('%Y%m%d_%H%M%S')}")
    improve(AnthropicLLM(), Policy.load(a.policy), run_dir, a.iterations, a.trials, a.confirm_trials,
            scenario_ids=a.scenarios.split(",") if a.scenarios else None)
    (run_dir / "usage.json").write_text(json.dumps(USAGE.snapshot(), indent=2), encoding="utf-8")
    print(f"report: {run_dir / 'REPORT.md'}")


if __name__ == "__main__":
    main()
