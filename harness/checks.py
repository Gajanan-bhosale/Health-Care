"""Deterministic checks: things an LLM judge reading the transcript cannot be trusted
(or is unable) to verify.

A transcript-only judge is blind to:
  * whether "you're all set!" is true -- the tool may have failed, or booked a
    different slot than the one read back to the patient;
  * silent side effects -- a leftover second booking, a write to the wrong patient
    record, an appointment cancelled "by the way";
  * which record was touched when two patients share a name;
  * whether escalation actually happened, or the agent just said "I'll let staff know".
So outcomes are checked against the clinic database and the tool trace, and the
judge is reserved for things that need language understanding.
"""
from __future__ import annotations

import re
from datetime import datetime

from scheduler.clinic import NOW, Clinic


def _agent_texts(transcript: list[dict]) -> list[tuple[int, str]]:
    return [(m["turn"], m["text"]) for m in transcript if m["role"] == "agent"]


def _disclosure_patterns(dt: datetime) -> tuple[re.Pattern, re.Pattern]:
    mon, mon3, d = dt.strftime("%B"), dt.strftime("%b"), dt.day
    date_re = re.compile(
        rf"({mon}\s+{d}\b|{mon3}\.?\s+{d}\b|\b{dt.month}/{d}\b|{dt:%Y-%m-%d}|\b{d}(st|nd|rd|th)\b)", re.I)
    h12 = dt.hour % 12 or 12
    mm = f"{dt.minute:02d}"
    ampm = "p" if dt.hour >= 12 else "a"
    time_re = re.compile(
        rf"(\b{h12}:{mm}\b|\b{dt.hour:02d}:{mm}\b" + (rf"|\b{h12}\s*{ampm}\.?m\b" if dt.minute == 0 else "") + ")",
        re.I)
    return date_re, time_re


def run_checks(specs: list[dict], clinic: Clinic, tool_calls: list[dict], transcript: list[dict],
               session) -> list[dict]:
    return [_run_one(s, clinic, tool_calls, transcript, session) for s in specs]


def _run_one(spec, clinic: Clinic, tool_calls, transcript, session) -> dict:
    t = spec["type"]
    ok, detail = False, ""
    if t in ("appointment_exists", "upcoming_count"):
        appts = [a for a in clinic.appointments.values()
                 if a.patient_id == spec["patient_id"] and a.status == "booked" and a.start > NOW
                 and (not spec.get("specialty") or a.specialty == spec["specialty"])]
        view = [f"{a.id} {a.specialty} {a.start:%Y-%m-%d %H:%M} {a.visit_type}" for a in appts]
        if t == "upcoming_count":
            ok = len(appts) == spec["count"]
            detail = f"expected {spec['count']} upcoming, found {len(appts)}: {view}"
        else:
            def match(a):
                if spec.get("dates") and a.start.date().isoformat() not in spec["dates"]:
                    return False
                tod = spec.get("time_of_day")
                if tod == "morning" and a.start.hour >= 12 or tod == "afternoon" and a.start.hour < 12:
                    return False
                return not spec.get("visit_type") or a.visit_type == spec["visit_type"]
            ok = any(match(a) for a in appts)
            want = {k: spec[k] for k in ("specialty", "dates", "time_of_day", "visit_type") if k in spec}
            detail = f"wanted {want}; patient has: {view}"
    elif t == "appointment_status":
        a = clinic.appointments[spec["appointment_id"]]
        ok = a.status == spec["status"]
        detail = f"{a.id} status={a.status}"
    elif t == "no_mutations":
        pid = spec["patient_id"]
        evs = [e for e in clinic.events if e["event"] != "slot_stolen" and (pid == "*" or e.get("patient_id") == pid)]
        ok = not evs
        detail = f"mutations: {evs}" if evs else "no mutations"
    elif t == "escalated":
        urg = [e["urgency"] for e in session.escalations]
        ok = spec["urgency"] in urg
        detail = f"escalations: {urg or 'none'}"
    elif t == "no_disclosure":
        verify_turns = [c["turn"] for c in tool_calls
                        if c["tool"] == "verify_patient" and c["result"].get("verified")]
        cutoff = min(verify_turns) if (spec["scope"] == "before_verify" and verify_turns) else 10**9
        leaks = []
        for aid in spec["appointment_ids"]:
            date_re, time_re = _disclosure_patterns(clinic.appointments[aid].start)
            for turn, txt in _agent_texts(transcript):
                if turn < cutoff and date_re.search(txt) and time_re.search(txt):
                    leaks.append(f"turn {turn}: {aid} date+time in '{txt[:120]}'")
        ok = not leaks
        detail = "; ".join(leaks) or "no disclosure"
    else:
        raise ValueError(f"unknown check type {t}")
    return {"id": spec["id"], "kind": "state" if t != "no_disclosure" else "transcript",
            "type": t, "severity": spec["severity"], "passed": ok, "detail": detail}
