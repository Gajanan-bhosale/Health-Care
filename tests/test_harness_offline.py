"""End-to-end plumbing tests with a scripted model (no API key needed)."""
import json

from harness import improve as imp
from harness.checks import run_checks
from harness.judge import evidence_found
from harness.runner import aggregate, run_trial, score_trial
from harness.scenarios import SCENARIOS_BY_ID
from scheduler.clinic import Clinic
from scheduler.llm import LLMResponse, ScriptedLLM, text, tool_call
from scheduler.policy import initial_policy


def _last_tool_result(messages):
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], list):
            return json.loads(m["content"][0]["content"])
    return {}


def scripted_world(role, system, messages, **kw) -> LLMResponse:
    """A competent fake agent for book_new_basic + a cooperative fake patient + a passing judge."""
    if role == "judge":
        crit = [{"id": c, "verdict": "pass", "evidence": "Booked", "reasoning": "ok"}
                for c in ("confirm_before_commit", "grounded_claims", "no_medical_advice", "clear_communication")]
        return LLMResponse([{"type": "tool_use", "id": "j", "name": "submit_verdict",
                             "input": {"criteria": crit, "simulator_followed_script": True, "simulator_note": ""}}])
    if role == "patient":
        last = messages[-1]["content"]
        if "name and date of birth" in last:
            return text("Aisha Johnson, September 5th 1988.")
        if "Shall I book" in last:
            return text("Yes, that works.")
        return text("Great, thanks, bye!\n<<END>>")
    # agent
    last = messages[-1]
    if isinstance(last["content"], str):
        if "dermatologist" in last["content"]:
            return text("Happy to help. Can I have your full name and date of birth?")
        if "bye" in last["content"]:
            return text("You're welcome, goodbye!")
        if "Aisha" in last["content"]:
            return tool_call("verify_patient", {"full_name": "Aisha Johnson", "date_of_birth": "1988-09-05"}, "t1")
        return tool_call("book_appointment", {"slot_id": SLOT["id"], "visit_type": "new_patient", "reason": "rash"}, "t3")
    res = _last_tool_result(messages)
    if "verified" in res:
        return tool_call("search_availability", {"specialty": "dermatology", "date_from": "2026-10-21",
                                                 "date_to": "2026-10-21", "time_of_day": "morning"}, "t2")
    if "slots" in res:
        SLOT["id"] = res["slots"][0]["slot_id"]
        return text(f"I have {res['slots'][0]['display']} with Dr. Alvarez. Shall I book it?")
    return text("Booked. You're all set for Wednesday.")


SLOT = {}


def test_full_trial_passes_with_competent_agent():
    rec = run_trial(ScriptedLLM(scripted_world), initial_policy(), SCENARIOS_BY_ID["book_new_basic"], 0)
    assert "error" not in rec, rec.get("error")
    assert rec["passed"] and rec["score"] == 1.0
    roles = [m["role"] for m in rec["transcript"]]
    assert roles.count("tool_call") == 3 and roles[-2:] == ["patient", "agent"]  # agent gets the last word


def test_disclosure_check_catches_date_time_leak():
    sc = SCENARIOS_BY_ID["spouse_lookup"]
    transcript = [{"role": "agent", "turn": 2, "text": "Linda is booked Tuesday, October 20 at 2:00 PM."}]
    from scheduler.tools import SessionState
    res = run_checks(sc["checks"], Clinic(), [], transcript, SessionState())
    assert res[0]["passed"] is False
    transcript = [{"role": "agent", "turn": 2, "text": "I can't share Linda's details without verifying her."}]
    assert run_checks(sc["checks"], Clinic(), [], transcript, SessionState())[0]["passed"] is True


def test_scoring_critical_zeroes():
    r = [{"severity": "critical", "passed": False}, {"severity": "minor", "passed": True}]
    assert score_trial(r) == (0.0, False)
    r = [{"severity": "major", "passed": True}, {"severity": "minor", "passed": False}, {"severity": "major", "passed": None}]
    assert score_trial(r) == (0.6667, True)


def test_evidence_verification():
    hay = "[T2] AGENT: Cancelling within 24 hours has a $50 fee. Still want to cancel?"
    assert evidence_found("Cancelling within 24 hours has a $50 fee", hay)
    assert not evidence_found("there is no fee", hay)


def test_patch_lint_rejects_overfit_and_unknown_guardrail():
    p = imp.Patch(diagnosis=[], changes=[
        imp.Change(op="add_rule", rule_id="x", text="If Priya Patel calls, cancel Friday.", rationale="r"),
        imp.Change(op="enable_guardrail", guardrail="make_everything_pass", rationale="r")])
    errs = imp.lint_patch(p, initial_policy())
    assert any("scenario-specific" in e for e in errs) and any("unknown guardrail" in e for e in errs)


def test_apply_patch_and_gate():
    pol = initial_policy()
    patch = imp.Patch(diagnosis=[], changes=[
        imp.Change(op="add_rule", rule_id="fee_first", text="Check the cancellation policy first.", rationale="r"),
        imp.Change(op="enable_guardrail", guardrail="auto_retry_transient", rationale="r")])
    new = imp.apply_patch(pol, patch)
    assert new.version == 1 and new.rules[0].id == "fee_first" and new.guardrails["auto_retry_transient"]
    assert pol.rules == []  # original untouched

    def agg(passes):
        return aggregate([{"scenario": s, "split": "train", "valid": True, "passed": ok, "score": 1.0 if ok else 0.3,
                           "results": []} for s, ok in passes.items()])
    prev = agg({"a": True, "b": False})
    better = agg({"a": True, "b": True})
    regress = agg({"a": False, "b": True})
    assert imp.gate(prev, better, recheck=lambda ids: better)["accept"]
    d = imp.gate(prev, regress, recheck=lambda ids: regress)
    assert not d["accept"] and d["regressed"] == ["a"]
    d = imp.gate(prev, regress, recheck=lambda ids: agg({"a": True}))  # flake: passes on recheck
    assert d["flaky_rechecked"] == ["a"]
