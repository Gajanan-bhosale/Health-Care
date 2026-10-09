"""Run scenarios against a policy and score them.

Scoring:
  severity weights critical=3, major=2, minor=1.
  trial score  = weighted share of applicable checks passed; 0 if any critical check fails.
  trial passed = no critical or major failure.
  A trial whose simulated patient went off-script is re-run once; if it is still off-script it
  is marked invalid and excluded from aggregates (unless no valid trial exists).
"""
from __future__ import annotations

import argparse
import json
import os
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from harness.checks import run_checks
from harness.judge import judge_transcript
from harness.patient_sim import PatientSimulator
from harness.scenarios import SCENARIOS, SCENARIOS_BY_ID
from scheduler.agent import SchedulingAgent
from scheduler.clinic import Clinic
from scheduler.policy import Policy

MAX_TURNS = 14
WEIGHTS = {"critical": 3, "major": 2, "minor": 1}
ROOT = Path(__file__).resolve().parent.parent


def build_transcript(agent: SchedulingAgent, final_patient: str | None) -> list[dict]:
    out, turn = [], 0
    for m in agent.messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            turn += 1
            out.append({"role": "patient", "turn": turn, "text": m["content"]})
        elif m["role"] == "assistant":
            for b in m["content"]:
                if b["type"] == "text" and b["text"].strip():
                    out.append({"role": "agent", "turn": turn, "text": b["text"].strip()})
                elif b["type"] == "tool_use":
                    out.append({"role": "tool_call", "turn": turn, "name": b["name"], "args": b["input"]})
        else:
            for b in m["content"]:
                out.append({"role": "tool_result", "turn": turn, "result": json.loads(b["content"]),
                            "is_error": b.get("is_error", False)})
    if final_patient:
        out.append({"role": "patient", "turn": turn + 1, "text": final_patient})
    return out


def score_trial(results: list[dict]) -> tuple[float, bool]:
    applicable = [r for r in results if r["passed"] is not None]
    if any(r["severity"] == "critical" and r["passed"] is False for r in applicable):
        return 0.0, False
    total = sum(WEIGHTS[r["severity"]] for r in applicable) or 1
    got = sum(WEIGHTS[r["severity"]] for r in applicable if r["passed"])
    passed = not any(r["passed"] is False and r["severity"] in ("critical", "major") for r in applicable)
    return round(got / total, 4), passed


def run_conversation(llm, policy: Policy, scenario: dict, seed: int) -> dict:
    c = scenario.get("clinic", {})
    clinic = Clinic(seed=seed, fully_booked=c.get("fully_booked"), open_dates=c.get("open_dates"),
                    faults=c.get("faults"))
    agent = SchedulingAgent(llm, policy, clinic)
    sim = PatientSimulator(llm, scenario)
    msg, end_reason = sim.opening, "max_turns"
    for _ in range(MAX_TURNS):
        reply = agent.respond(msg)
        msg, done = sim.reply(reply)
        if done:
            end_reason = "patient_ended"
            break
    # The agent always answers the patient's last line (e.g. "Yes, book it. Bye!"), as on a real call;
    # otherwise a confirmation said together with a goodbye could never be acted on.
    agent.respond(msg)
    transcript = build_transcript(agent, None)
    checks = run_checks(scenario["checks"], clinic, agent.tools.calls, transcript, agent.session)
    return {"transcript": transcript, "checks": checks, "end_reason": end_reason,
            "events": clinic.events, "escalations": agent.session.escalations}


def run_trial(llm, policy: Policy, scenario: dict, trial: int) -> dict:
    t0 = time.time()
    attempts = []
    for attempt in range(2):
        try:
            conv = run_conversation(llm, policy, scenario, seed=7 + trial)
            verdict = judge_transcript(llm, scenario, conv["transcript"])
        except Exception as e:  # noqa: BLE001 - infra errors must not count as agent failures
            attempts.append({"error": f"{type(e).__name__}: {e}", "tb": traceback.format_exc()[-1500:]})
            continue
        results = conv["checks"] + verdict["criteria"]
        score, passed = score_trial(results)
        rec = {"scenario": scenario["id"], "split": scenario["split"], "trial": trial,
               "policy_version": policy.version, "valid": verdict["simulator_followed_script"],
               "simulator_note": verdict["simulator_note"], "score": score, "passed": passed,
               "results": results, **conv, "attempt": attempt + 1}
        attempts.append(rec)
        if rec["valid"]:
            break
    final = next((a for a in reversed(attempts) if "score" in a), None)
    if final is None:
        return {"scenario": scenario["id"], "split": scenario["split"], "trial": trial,
                "policy_version": policy.version, "valid": False, "error": attempts[-1]["error"],
                "score": 0.0, "passed": False, "results": [], "seconds": round(time.time() - t0, 1)}
    final["seconds"] = round(time.time() - t0, 1)
    return final


def aggregate(trials: list[dict]) -> dict:
    by_s: dict[str, list[dict]] = {}
    for t in trials:
        by_s.setdefault(t["scenario"], []).append(t)
    scen = {}
    for sid, ts in by_s.items():
        valid = [t for t in ts if t.get("valid") and "error" not in t] or ts
        fails: dict[str, int] = {}
        for t in valid:
            for r in t["results"]:
                if r["passed"] is False:
                    fails[f"{r['id']} ({r['severity']})"] = fails.get(f"{r['id']} ({r['severity']})", 0) + 1
        scen[sid] = {"split": ts[0]["split"], "trials": len(ts), "valid_trials": len(valid),
                     "pass_rate": round(sum(t["passed"] for t in valid) / len(valid), 3),
                     "score": round(sum(t["score"] for t in valid) / len(valid), 4),
                     "failed_checks": fails,
                     "errors": [t["error"] for t in ts if "error" in t]}
    summary = {}
    for split in ("train", "holdout", "all"):
        rows = [v for v in scen.values() if split == "all" or v["split"] == split]
        if rows:
            summary[split] = {"scenarios": len(rows),
                              "mean_score": round(sum(r["score"] for r in rows) / len(rows), 4),
                              "mean_pass_rate": round(sum(r["pass_rate"] for r in rows) / len(rows), 4),
                              "fully_passing": sum(r["pass_rate"] == 1.0 for r in rows)}
    return {"summary": summary, "scenarios": dict(sorted(scen.items()))}


def run_suite(llm, policy: Policy, out_dir: Path, trials: int = 1, scenario_ids: list[str] | None = None,
              workers: int | None = None) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    scenarios = [SCENARIOS_BY_ID[s] for s in scenario_ids] if scenario_ids else SCENARIOS
    jobs = [(s, k) for s in scenarios for k in range(trials)]
    workers = workers or int(os.environ.get("WORKERS", "6"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        recs = list(ex.map(lambda j: run_trial(llm, policy, j[0], j[1]), jobs))
    for r in recs:
        (out_dir / f"{r['scenario']}__t{r['trial']}.json").write_text(json.dumps(r, indent=2, default=str),
                                                                      encoding="utf-8")
    agg = aggregate(recs)
    agg["policy"] = policy.summary()
    (out_dir / "suite.json").write_text(json.dumps(agg, indent=2), encoding="utf-8")
    return agg


def main() -> None:
    from scheduler.llm import USAGE, AnthropicLLM
    ap = argparse.ArgumentParser(description="Run the scenario suite against a policy.")
    ap.add_argument("--policy", default=str(ROOT / "policies" / "policy_v0.json"))
    ap.add_argument("--trials", type=int, default=1)
    ap.add_argument("--scenarios", help="comma-separated scenario ids")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    policy = Policy.load(a.policy)
    out = Path(a.out or ROOT / "runs" / f"eval_{time.strftime('%Y%m%d_%H%M%S')}_v{policy.version}")
    agg = run_suite(AnthropicLLM(), policy, out, a.trials, a.scenarios.split(",") if a.scenarios else None)
    print(json.dumps({"out": str(out), **agg["summary"], "usage": USAGE.snapshot()}, indent=2))
    for sid, s in agg["scenarios"].items():
        print(f"{'PASS' if s['pass_rate'] == 1 else 'FAIL'}  {s['score']:.2f}  {sid:34s} {s['failed_checks'] or ''}")


if __name__ == "__main__":
    main()
