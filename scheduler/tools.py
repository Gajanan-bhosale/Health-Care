"""Tools exposed to the agent, and the code-level guardrails around them.

Scoping decisions (deliberate):
  * No tool accepts a patient_id. Patient-scoped tools act on whoever the session
    has verified, so the model cannot read or write another patient's record by
    passing a different id, whatever a caller talks it into.
  * verify_patient returns the same answer for "wrong DOB" and "no such person"
    so it cannot be used to enumerate patients, and locks after 3 failures.
  * Errors come back as structured {code, message, retryable} so the agent can
    branch on them instead of parsing prose.
  * Some guardrails are switchable via the policy file. They are part of what the
    improvement loop is allowed to change, and each has a cost (see GUARDRAILS).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .clinic import (NOW, POLICIES, PROVIDERS, PROVIDERS_BY_ID, SPECIALTIES, TODAY, VISIT_TYPES,
                     BOOKING_HORIZON_DAYS, Clinic, ClinicError, display_dt, parse_slot_id)

MAX_VERIFY_ATTEMPTS = 3
SEARCH_LIMIT = 8

# Switchable guardrails. The optimizer sees this menu, including the trade-offs.
GUARDRAILS: dict[str, str] = {
    "emergency_screen": (
        "Before each agent turn, scan the patient's message for red-flag symptoms (chest pain, trouble "
        "breathing, stroke signs, severe bleeding, suicidal statements) and inject a high-priority "
        "notice into the agent's context. Cost: keyword false positives (e.g. 'my chest pain from last "
        "year was fine')."),
    "auto_retry_transient": (
        "The tool layer automatically retries a tool call up to 2 times when it fails with a retryable "
        "error (e.g. TIMEOUT) before returning the error to the agent. Cost: added latency."),
    "block_duplicate_specialty_booking": (
        "book_appointment refuses to create a second upcoming appointment in a specialty where the "
        "patient already has one, returning DUPLICATE_BOOKING and pointing at reschedule. Cost: "
        "patients who genuinely need two visits must be handled by staff."),
}

RED_FLAGS = [
    r"chest (pain|pressure|tightness)", r"crushing", r"can'?t breathe", r"(trouble|difficulty) breathing",
    r"short(ness)? of breath", r"numb(ness)?", r"face (is )?droop", r"slurred", r"stroke",
    r"(severe|heavy|won'?t stop) bleeding", r"suicid", r"kill myself", r"overdos", r"unconscious",
    r"passed out", r"faint(ed|ing)",
]


def red_flags(text: str) -> list[str]:
    t = text.lower()
    return [p for p in RED_FLAGS if re.search(p, t)]


@dataclass
class SessionState:
    """Conversation state owned by code, not inferred from the transcript."""
    verified_patient_id: str | None = None
    verified_name: str | None = None
    failed_verifications: int = 0
    locked: bool = False
    escalations: list[dict] = field(default_factory=list)
    slots_shown: set[str] = field(default_factory=set)

    def render(self) -> str:
        lines = [f"- Current date/time: {display_dt(NOW)} (today is {TODAY:%A, %B %d, %Y})."]
        if self.verified_patient_id:
            lines.append(f"- Caller verified as: {self.verified_name}. Appointment tools act on this patient only.")
        else:
            lines.append("- Caller identity: NOT verified.")
        if self.locked:
            lines.append("- Verification LOCKED after repeated failures: do not attempt again; offer a transfer to staff.")
        elif self.failed_verifications:
            lines.append(f"- Failed verification attempts: {self.failed_verifications}/{MAX_VERIFY_ATTEMPTS}.")
        if self.escalations:
            lines.append(f"- Escalations filed this call: {', '.join(e['urgency'] for e in self.escalations)}.")
        return "\n".join(lines)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "verify_patient",
        "description": "Verify the caller's identity using full name and date of birth. Required before any "
                       "appointment lookup or change. On success, all appointment tools act on this patient.",
        "input_schema": {"type": "object", "properties": {
            "full_name": {"type": "string"},
            "date_of_birth": {"type": "string", "description": "YYYY-MM-DD"}},
            "required": ["full_name", "date_of_birth"]},
    },
    {
        "name": "get_my_appointments",
        "description": "List upcoming appointments for the verified patient.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "search_availability",
        "description": "Find open appointment slots. Returns up to 8 slots, earliest first.",
        "input_schema": {"type": "object", "properties": {
            "specialty": {"type": "string", "enum": SPECIALTIES + ["other"],
                          "description": "Use 'other' to check a specialty not in the list."},
            "date_from": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "date_to": {"type": "string", "description": "YYYY-MM-DD, inclusive"},
            "provider_name": {"type": "string", "description": "Optional, e.g. 'Dr. Rao'"},
            "time_of_day": {"type": "string", "enum": ["morning", "afternoon", "any"]}},
            "required": ["specialty", "date_from", "date_to"]},
    },
    {
        "name": "book_appointment",
        "description": "Book a slot (from search_availability) for the verified patient.",
        "input_schema": {"type": "object", "properties": {
            "slot_id": {"type": "string"},
            "visit_type": {"type": "string", "enum": VISIT_TYPES},
            "reason": {"type": "string", "description": "The patient's own brief description of why they "
                       "are coming in (e.g. 'sore throat for a week')"}},
            "required": ["slot_id", "visit_type", "reason"]},
    },
    {
        "name": "reschedule_appointment",
        "description": "Move one of the verified patient's appointments to a new slot (same specialty). "
                       "Atomic: the old time is released only if the new one is secured.",
        "input_schema": {"type": "object", "properties": {
            "appointment_id": {"type": "string"}, "new_slot_id": {"type": "string"}},
            "required": ["appointment_id", "new_slot_id"]},
    },
    {
        "name": "cancel_appointment",
        "description": "Cancel one of the verified patient's appointments.",
        "input_schema": {"type": "object", "properties": {"appointment_id": {"type": "string"}},
                         "required": ["appointment_id"]},
    },
    {
        "name": "get_clinic_policy",
        "description": "Look up clinic policy text.",
        "input_schema": {"type": "object", "properties": {
            "topic": {"type": "string", "enum": sorted(POLICIES)}}, "required": ["topic"]},
    },
    {
        "name": "escalate_to_human",
        "description": "Hand off to clinic staff. Use urgency 'emergency' for possible medical emergencies, "
                       "'urgent' for clinical questions needing a nurse today, 'routine' for anything else "
                       "you cannot handle.",
        "input_schema": {"type": "object", "properties": {
            "urgency": {"type": "string", "enum": ["emergency", "urgent", "routine"]},
            "reason": {"type": "string"}},
            "required": ["urgency", "reason"]},
    },
]

WRITE_TOOLS = {"book_appointment", "reschedule_appointment", "cancel_appointment"}
NEEDS_VERIFICATION = WRITE_TOOLS | {"get_my_appointments"}


class ToolExecutor:
    def __init__(self, clinic: Clinic, session: SessionState, guardrails: dict[str, bool]):
        self.clinic = clinic
        self.session = session
        self.guardrails = guardrails
        self.calls: list[dict] = []  # full trace, used by deterministic checks and the judge

    def execute(self, name: str, args: dict, turn: int) -> tuple[dict, bool]:
        attempts = 3 if self.guardrails.get("auto_retry_transient") else 1
        for attempt in range(attempts):
            result, is_error = self._execute_once(name, args)
            retryable = is_error and result.get("error", {}).get("retryable")
            if not retryable or attempt == attempts - 1:
                break
        self.calls.append({"turn": turn, "tool": name, "args": args, "result": result,
                           "is_error": is_error, "attempts": attempt + 1})
        return result, is_error

    def _execute_once(self, name: str, args: dict) -> tuple[dict, bool]:
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return _err("UNKNOWN_TOOL", f"No tool named {name}."), True
        if name in NEEDS_VERIFICATION and not self.session.verified_patient_id:
            return _err("NOT_VERIFIED", "Verify the caller with verify_patient first."), True
        try:
            return handler(**args), False
        except ClinicError as e:
            return _err(e.code, e.message, e.retryable), True
        except (TypeError, ValueError) as e:
            return _err("INVALID_ARGUMENT", str(e)), True

    # ---- handlers ----
    def _t_verify_patient(self, full_name: str, date_of_birth: str) -> dict:
        s = self.session
        if s.locked:
            return {"verified": False, "locked": True,
                    "message": "Verification is locked for this call. Offer to transfer to staff."}
        _parse_date(date_of_birth)
        p = self.clinic.find_patient(full_name, date_of_birth)
        if p is None:
            s.failed_verifications += 1
            s.locked = s.failed_verifications >= MAX_VERIFY_ATTEMPTS
            return {"verified": False, "locked": s.locked,
                    "message": "No patient matches that name and date of birth together."}
        s.verified_patient_id, s.verified_name = p.id, p.name
        s.failed_verifications = 0
        return {"verified": True, "patient_name": p.name, "new_patient": p.new_patient,
                "note": "No prior visits on file: book visit_type new_patient." if p.new_patient else ""}

    def _t_get_my_appointments(self) -> dict:
        appts = self.clinic.upcoming(self.session.verified_patient_id)
        return {"appointments": [_appt_view(a) for a in appts]}

    def _t_search_availability(self, specialty: str, date_from: str, date_to: str,
                               provider_name: str | None = None, time_of_day: str | None = None) -> dict:
        d0, d1 = _parse_date(date_from), _parse_date(date_to)
        if d1 < d0:
            raise ValueError("date_to is before date_from.")
        if d1 < TODAY:
            raise ValueError(f"Dates are in the past. Today is {TODAY.isoformat()}.")
        if d0 > TODAY + timedelta(days=BOOKING_HORIZON_DAYS):
            raise ClinicError("OUT_OF_RANGE", f"We only book {BOOKING_HORIZON_DAYS} days ahead.")
        pid = None
        if provider_name:
            key = provider_name.lower().replace("dr.", "").replace("dr ", "").strip()
            match = [p for p in PROVIDERS if key and key in p.name.lower()]
            if not match:
                raise ClinicError("UNKNOWN_PROVIDER", f"No provider named {provider_name}. "
                                  f"Providers: {', '.join(p.name + ' (' + p.specialty + ')' for p in PROVIDERS)}.")
            pid = match[0].id
        tod = None if time_of_day in (None, "any") else time_of_day
        if specialty == "other":
            raise ClinicError("UNSUPPORTED_SPECIALTY", POLICIES["specialties"])
        found = self.clinic.search(specialty, d0, d1, pid, tod, limit=10_000)
        slots = found[:SEARCH_LIMIT]
        self.session.slots_shown.update(s["slot_id"] for s in slots)
        more = len(found) - len(slots)
        note = ("No open slots in that range." if not slots else
                f"Showing the earliest {len(slots)} of {len(found)} open slots; {more} later ones are not "
                "shown. Don't say these are all the openings; narrow the dates or time of day to see others."
                if more else "These are all the open slots in that range.")
        return {"slots": slots, "total_open": len(found), "note": note}

    def _t_book_appointment(self, slot_id: str, visit_type: str, reason: str) -> dict:
        if visit_type not in VISIT_TYPES:
            raise ValueError(f"visit_type must be one of {VISIT_TYPES}")
        pid = self.session.verified_patient_id
        if self.guardrails.get("block_duplicate_specialty_booking"):
            spec = PROVIDERS_BY_ID.get(parse_slot_id(slot_id)[0])
            dupes = [a for a in self.clinic.upcoming(pid) if spec and a.specialty == spec.specialty]
            if dupes:
                raise ClinicError("DUPLICATE_BOOKING",
                                  f"Patient already has an upcoming {spec.specialty} appointment "
                                  f"({dupes[0].id}, {display_dt(dupes[0].start)}). Ask whether to "
                                  "reschedule that one instead.")
        appt = self.clinic.book(pid, slot_id, visit_type, reason)
        return {"booked": True, "appointment": _appt_view(appt)}

    def _t_reschedule_appointment(self, appointment_id: str, new_slot_id: str) -> dict:
        appt, late = self.clinic.reschedule(self.session.verified_patient_id, appointment_id, new_slot_id)
        return {"rescheduled": True, "appointment": _appt_view(appt), "late_change": late}

    def _t_cancel_appointment(self, appointment_id: str) -> dict:
        appt, late = self.clinic.cancel(self.session.verified_patient_id, appointment_id)
        out = {"cancelled": True, "appointment_id": appt.id, "late_cancellation": late}
        if late:
            out["fee_applied"] = "$50 late-cancellation fee"
        return out

    def _t_get_clinic_policy(self, topic: str) -> dict:
        if topic not in POLICIES:
            raise ValueError(f"topic must be one of {sorted(POLICIES)}")
        return {"topic": topic, "policy": POLICIES[topic]}

    def _t_escalate_to_human(self, urgency: str, reason: str) -> dict:
        if urgency not in ("emergency", "urgent", "routine"):
            raise ValueError("urgency must be emergency, urgent or routine")
        self.session.escalations.append({"urgency": urgency, "reason": reason})
        msg = {"emergency": "Emergency flagged to on-site staff. Tell the caller to call 911 now.",
               "urgent": "Nurse line ticket created; a nurse will call back within 1 hour.",
               "routine": "Front-desk ticket created; staff will call back within 1 business day."}[urgency]
        return {"escalated": True, "ticket": f"T-{len(self.session.escalations):03d}", "instructions": msg}


def _err(code: str, message: str, retryable: bool = False) -> dict:
    return {"error": {"code": code, "message": message, "retryable": retryable}}


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s)
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"'{s}' is not a YYYY-MM-DD date.") from e


def _appt_view(a) -> dict:
    return {"appointment_id": a.id, "provider": PROVIDERS_BY_ID[a.provider_id].name,
            "specialty": a.specialty, "start": a.start.isoformat(timespec="minutes"),
            "display": display_dt(a.start), "visit_type": a.visit_type}
