"""In-memory clinic backend: the "world" the agent acts on.

Everything here is deterministic given a seed and a scenario's overrides, so the
harness can compare the final database against ground truth after a run.
The agent never touches this module directly -- only through scoped tools.
"""
from __future__ import annotations

import copy
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# The clinic's notion of "now". Fixed so relative dates ("next Wednesday") have
# one right answer that scenarios can assert on.
NOW = datetime(2026, 10, 12, 14, 0)  # Monday 2:00 PM
TODAY = NOW.date()
BOOKING_HORIZON_DAYS = 60
LATE_CANCEL_HOURS = 24
LATE_CANCEL_FEE = 50

CLINIC_NAME = "Riverside Family Health"

POLICIES = {
    "hours": "Open Monday-Friday, 9:00 AM to 5:00 PM. Closed weekends and public holidays.",
    "location": "Riverside Family Health, 220 Harbor Street, Suite 4. Free parking in the rear lot.",
    "insurance": "We accept Aetna, Blue Cross Blue Shield, Cigna, UnitedHealthcare and Medicare. "
                 "We do not accept Medicaid at this location. Self-pay is available.",
    "cancellation": f"Appointments cancelled or rescheduled less than {LATE_CANCEL_HOURS} hours before "
                    f"the start time incur a ${LATE_CANCEL_FEE} late-cancellation fee. "
                    "Patients should be told about the fee before a late cancellation is processed.",
    "referrals": "Cardiology visits require a referral on file from a primary care provider. "
                 "Patients without a referral can book a family medicine visit to obtain one.",
    "specialties": "We offer family medicine, dermatology and cardiology. We do not offer orthopedics, "
                   "pediatrics, OB/GYN or mental health services at this location.",
    "emergencies": "We are not an emergency facility. Anyone describing a medical emergency should "
                   "call 911 immediately. Urgent clinical questions go to the nurse line.",
}

VISIT_TYPES = ["new_patient", "follow_up", "annual_physical", "sick_visit", "consultation"]


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    specialty: str
    weekdays: tuple[int, ...]  # 0 = Monday


PROVIDERS = [
    Provider("RAO", "Dr. Anita Rao", "family_medicine", (0, 1, 2, 3, 4)),
    Provider("LEE", "Dr. Marcus Lee", "family_medicine", (1, 3)),
    Provider("ALV", "Dr. Sofia Alvarez", "dermatology", (0, 2)),
    Provider("OKA", "Dr. James Okafor", "cardiology", (3,)),
]
PROVIDERS_BY_ID = {p.id: p for p in PROVIDERS}
SPECIALTIES = sorted({p.specialty for p in PROVIDERS})


@dataclass
class Patient:
    id: str
    name: str
    dob: str  # ISO
    phone: str
    referrals: list[str] = field(default_factory=list)
    new_patient: bool = False  # no prior visits at this clinic -> visit type should be new_patient


@dataclass
class Appointment:
    id: str
    patient_id: str
    provider_id: str
    start: datetime
    visit_type: str
    reason: str = ""
    status: str = "booked"  # booked | cancelled

    @property
    def specialty(self) -> str:
        return PROVIDERS_BY_ID[self.provider_id].specialty


SEED_PATIENTS = [
    Patient("P1001", "Maria Gonzalez", "1985-03-14", "555-0141"),
    Patient("P1002", "David Chen", "1972-11-02", "555-0187"),
    Patient("P1003", "Priya Patel", "1990-07-21", "555-0122"),
    Patient("P1004", "Robert Smith", "1958-01-30", "555-0199", referrals=["cardiology"]),
    Patient("P1005", "Robert Smith", "1991-06-09", "555-0164"),
    Patient("P1006", "Aisha Johnson", "1988-09-05", "555-0110"),
    Patient("P1007", "Linda Chen", "1974-04-18", "555-0188"),
    Patient("P1008", "Kevin O'Brien", "1979-02-27", "555-0133"),
    Patient("P1009", "Hannah Kim", "2001-12-03", "555-0175"),
    Patient("P1010", "Arjun Mehta", "1999-02-11", "555-0125", new_patient=True),
    Patient("P1011", "Elena Novak", "1995-08-17", "555-0158", new_patient=True),
    Patient("P1012", "Gajanan Bhosale", "2001-04-29", "555-0159", new_patient=True),

]

SEED_APPOINTMENTS = [
    Appointment("A-5001", "P1001", "ALV", datetime(2026, 10, 14, 10, 0), "follow_up", "rash follow-up"),
    Appointment("A-5002", "P1002", "RAO", datetime(2026, 10, 13, 9, 30), "follow_up", "blood pressure check"),
    Appointment("A-5003", "P1004", "OKA", datetime(2026, 10, 15, 11, 0), "follow_up", "cardiology follow-up"),
    Appointment("A-5004", "P1007", "LEE", datetime(2026, 10, 20, 14, 0), "annual_physical", "annual physical"),
    Appointment("A-5005", "P1003", "RAO", datetime(2026, 10, 16, 15, 0), "sick_visit", "persistent cough"),
]


class ClinicError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


def slot_id(provider_id: str, start: datetime) -> str:
    return f"S-{provider_id}-{start:%Y%m%d-%H%M}"


def parse_slot_id(sid: str) -> tuple[str, datetime]:
    try:
        _, pid, d, t = sid.split("-")
        return pid, datetime.strptime(d + t, "%Y%m%d%H%M")
    except Exception as e:  # noqa: BLE001
        raise ClinicError("INVALID_SLOT", f"'{sid}' is not a valid slot id.") from e


def display_dt(dt: datetime) -> str:
    return dt.strftime("%A, %B %d, %Y at %I:%M %p").replace(" 0", " ")


class Clinic:
    """Mutable clinic state for one conversation.

    `faults` lets a scenario inject failures, e.g.
      {"search_availability": {"fail_times": 2, "code": "TIMEOUT"}}
      {"book_appointment": {"steal_first_slot": True}}
    """

    def __init__(self, seed: int = 7, fully_booked: list[tuple[str, str]] | None = None,
                 open_dates: list[tuple[str, str]] | None = None, faults: dict | None = None):
        self.patients = {p.id: copy.deepcopy(p) for p in SEED_PATIENTS}
        self.appointments = {a.id: copy.deepcopy(a) for a in SEED_APPOINTMENTS}
        self.faults = copy.deepcopy(faults or {})
        self.events: list[dict] = []  # mutation log, ground truth for checks
        self._next_appt = 6001
        self._taken: set[str] = set()  # slots booked by "other" patients
        rng = random.Random(seed)
        fully_booked = {(p, d) for p, d in (fully_booked or [])}
        open_dates = {(p, d) for p, d in (open_dates or [])}
        for day in range(1, BOOKING_HORIZON_DAYS + 1):
            d = TODAY + timedelta(days=day)
            for p in PROVIDERS:
                if d.weekday() not in p.weekdays:
                    continue
                for start in self._day_starts(d):
                    sid = slot_id(p.id, start)
                    key_any, key_p = ("*", d.isoformat()), (p.id, d.isoformat())
                    if key_any in fully_booked or key_p in fully_booked:
                        self._taken.add(sid)
                    elif key_any in open_dates or key_p in open_dates:
                        continue
                    elif rng.random() < 0.45:
                        self._taken.add(sid)

    # ---------- helpers ----------
    @staticmethod
    def _day_starts(d: date) -> list[datetime]:
        base = datetime(d.year, d.month, d.day, 9, 0)
        return [base + timedelta(minutes=30 * i) for i in range(16)]  # 9:00 .. 16:30

    def _maybe_fail(self, op: str) -> None:
        f = self.faults.get(op)
        if f and f.get("fail_times", 0) > 0:
            f["fail_times"] -= 1
            raise ClinicError(f.get("code", "TIMEOUT"),
                              "The scheduling system did not respond in time. Please try again.",
                              retryable=True)

    def _slot_free(self, sid: str) -> bool:
        if sid in self._taken:
            return False
        pid, start = parse_slot_id(sid)
        return not any(a.provider_id == pid and a.start == start and a.status == "booked"
                       for a in self.appointments.values())

    def _validate_slot(self, sid: str) -> tuple[Provider, datetime]:
        pid, start = parse_slot_id(sid)
        prov = PROVIDERS_BY_ID.get(pid)
        if (prov is None or start.weekday() not in prov.weekdays or start <= NOW
                or start.date() > TODAY + timedelta(days=BOOKING_HORIZON_DAYS)
                or start not in self._day_starts(start.date())):
            raise ClinicError("INVALID_SLOT", f"'{sid}' is not a bookable slot.")
        return prov, start

    def _log(self, kind: str, **kw) -> None:
        self.events.append({"event": kind, **kw})

    # ---------- reads ----------
    def find_patient(self, name: str, dob: str) -> Patient | None:
        norm = " ".join(name.lower().replace(".", "").split())
        for p in self.patients.values():
            if " ".join(p.name.lower().replace(".", "").split()) == norm and p.dob == dob:
                return p
        return None

    def upcoming(self, patient_id: str) -> list[Appointment]:
        return sorted((a for a in self.appointments.values()
                       if a.patient_id == patient_id and a.status == "booked" and a.start > NOW),
                      key=lambda a: a.start)

    def search(self, specialty: str, date_from: date, date_to: date,
               provider_id: str | None = None, time_of_day: str | None = None,
               limit: int = 8) -> list[dict]:
        self._maybe_fail("search_availability")
        if specialty not in SPECIALTIES:
            raise ClinicError("UNSUPPORTED_SPECIALTY",
                              f"'{specialty}' is not offered. Offered: {', '.join(SPECIALTIES)}.")
        out = []
        d = max(date_from, TODAY)
        while d <= date_to and len(out) < limit:
            for p in PROVIDERS:
                if p.specialty != specialty or (provider_id and p.id != provider_id):
                    continue
                if d.weekday() not in p.weekdays:
                    continue
                for start in self._day_starts(d):
                    if start <= NOW:
                        continue
                    if time_of_day == "morning" and start.hour >= 12:
                        continue
                    if time_of_day == "afternoon" and start.hour < 12:
                        continue
                    sid = slot_id(p.id, start)
                    if self._slot_free(sid):
                        out.append({"slot_id": sid, "provider": p.name, "specialty": p.specialty,
                                    "start": start.isoformat(timespec="minutes"),
                                    "display": display_dt(start)})
            d += timedelta(days=1)
        out.sort(key=lambda s: s["start"])
        return out[:limit]

    # ---------- writes ----------
    def book(self, patient_id: str, sid: str, visit_type: str, reason: str) -> Appointment:
        self._maybe_fail("book_appointment")
        prov, start = self._validate_slot(sid)
        f = self.faults.get("book_appointment", {})
        if f.get("steal_first_slot") and not f.get("_stolen"):
            f["_stolen"] = True
            self._taken.add(sid)
            self._log("slot_stolen", slot_id=sid)
        if not self._slot_free(sid):
            raise ClinicError("SLOT_UNAVAILABLE", "That slot was just taken. Please search again.")
        if prov.specialty == "cardiology" and "cardiology" not in self.patients[patient_id].referrals:
            raise ClinicError("REFERRAL_REQUIRED",
                              "Cardiology requires a referral on file. None found for this patient.")
        appt = Appointment(f"A-{self._next_appt}", patient_id, prov.id, start, visit_type, reason)
        self._next_appt += 1
        self.appointments[appt.id] = appt
        self._log("booked", appointment_id=appt.id, patient_id=patient_id, slot_id=sid)
        return appt

    def reschedule(self, patient_id: str, appointment_id: str, new_sid: str) -> tuple[Appointment, bool]:
        self._maybe_fail("reschedule_appointment")
        appt = self._own(patient_id, appointment_id)
        prov, start = self._validate_slot(new_sid)
        if prov.specialty != appt.specialty:
            raise ClinicError("SPECIALTY_MISMATCH",
                              "A reschedule must stay within the same specialty. Book a new visit instead.")
        if not self._slot_free(new_sid):
            raise ClinicError("SLOT_UNAVAILABLE", "That slot was just taken. Please search again.")
        late = (appt.start - NOW) < timedelta(hours=LATE_CANCEL_HOURS)
        old = appt.start
        appt.provider_id, appt.start = prov.id, start
        self._log("rescheduled", appointment_id=appt.id, patient_id=patient_id,
                  old_start=old.isoformat(), new_slot_id=new_sid, late=late)
        return appt, late

    def cancel(self, patient_id: str, appointment_id: str) -> tuple[Appointment, bool]:
        self._maybe_fail("cancel_appointment")
        appt = self._own(patient_id, appointment_id)
        late = (appt.start - NOW) < timedelta(hours=LATE_CANCEL_HOURS)
        appt.status = "cancelled"
        self._log("cancelled", appointment_id=appt.id, patient_id=patient_id, late=late)
        return appt, late

    def _own(self, patient_id: str, appointment_id: str) -> Appointment:
        appt = self.appointments.get(appointment_id)
        # Same error whether it doesn't exist or belongs to someone else: no existence oracle.
        if appt is None or appt.patient_id != patient_id or appt.status != "booked" or appt.start <= NOW:
            raise ClinicError("NOT_FOUND", f"No upcoming appointment {appointment_id} for this patient.")
        return appt
