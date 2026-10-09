"""Talk to the scheduling agent yourself, as the patient, in the terminal.

    python -m scheduler.chat                                   # learned policy
    python -m scheduler.chat --policy policies/policy_v0.json  # baseline
    python -m scheduler.chat --fault search_outage             # inject a failure

Tool calls are printed as they happen. Commands: /state (session + your appointments), /quit.
Test patients (name / DOB): Maria Gonzalez 1985-03-14, David Chen 1972-11-02, Priya Patel 1990-07-21,
Robert Smith 1958-01-30 and 1991-06-09, Aisha Johnson 1988-09-05, Linda Chen 1974-04-18,
Kevin O'Brien 1979-02-27, Hannah Kim 2001-12-03,
Arjun Mehta 1999-02-11 (new patient), Elena Novak 1995-08-17 (new patient).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import SchedulingAgent
from .clinic import NOW, PROVIDERS_BY_ID, Clinic, display_dt
from .llm import AnthropicLLM
from .policy import POLICY_DIR, Policy

FAULTS = {
    "none": {},
    "search_outage": {"search_availability": {"fail_times": 2, "code": "TIMEOUT"}},
    "slot_race": {"book_appointment": {"steal_first_slot": True}},
}


def main() -> None:
    ap = argparse.ArgumentParser(description="Chat with the scheduling agent as a patient.")
    ap.add_argument("--policy", default=str(POLICY_DIR / "policy_v3_learned.json"))
    ap.add_argument("--fault", choices=list(FAULTS), default="none")
    a = ap.parse_args()

    policy = Policy.load(a.policy)
    clinic = Clinic(faults=FAULTS[a.fault])
    agent = SchedulingAgent(AnthropicLLM(), policy, clinic)
    print(f"Riverside Family Health - policy v{policy.version} ({len(policy.rules)} rules), fault: {a.fault}")
    print(f"Clinic clock: {display_dt(NOW)}. Type as the patient; /state, /quit.\n")

    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not msg:
            continue
        if msg == "/quit":
            break
        if msg == "/state":
            show_state(agent, clinic)
            continue
        seen = len(agent.tools.calls)
        reply = agent.respond(msg)
        for c in agent.tools.calls[seen:]:
            status = "ERROR " if c["is_error"] else ""
            print(f"   [tool] {c['tool']}({json.dumps(c['args'])}) -> {status}{json.dumps(c['result'])[:200]}")
        print(f"agent> {reply}\n")


def show_state(agent: SchedulingAgent, clinic: Clinic) -> None:
    print(agent.session.render())
    pid = agent.session.verified_patient_id
    if pid:
        for ap in clinic.upcoming(pid):
            print(f"   {ap.id}: {PROVIDERS_BY_ID[ap.provider_id].name}, {display_dt(ap.start)} ({ap.visit_type})")
    for e in clinic.events:
        print(f"   event: {e}")
    print()


if __name__ == "__main__":
    main()
