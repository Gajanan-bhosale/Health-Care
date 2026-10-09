from scheduler.clinic import Clinic
from scheduler.tools import SessionState, ToolExecutor, red_flags

GUARDS_OFF = {"emergency_screen": False, "auto_retry_transient": False, "block_duplicate_specialty_booking": False}


def make(guards=None, **clinic_kw):
    s = SessionState()
    return ToolExecutor(Clinic(**clinic_kw), s, {**GUARDS_OFF, **(guards or {})}), s


def call(ex, name, **args):
    return ex.execute(name, args, turn=1)


def test_patient_tools_require_verification():
    ex, _ = make()
    for name, args in [("get_my_appointments", {}), ("cancel_appointment", {"appointment_id": "A-5001"})]:
        out, err = call(ex, name, **args)
        assert err and out["error"]["code"] == "NOT_VERIFIED"


def test_verification_gives_no_enumeration_oracle_and_locks():
    ex, s = make()
    a, _ = call(ex, "verify_patient", full_name="Maria Gonzalez", date_of_birth="1985-03-15")  # wrong dob
    b, _ = call(ex, "verify_patient", full_name="Nobody Real", date_of_birth="1985-03-14")      # no such name
    assert a["message"] == b["message"] and not a["verified"]
    c, _ = call(ex, "verify_patient", full_name="Maria Gonzalez", date_of_birth="2000-01-01")
    assert c["locked"] and s.locked
    d, _ = call(ex, "verify_patient", full_name="Maria Gonzalez", date_of_birth="1985-03-14")  # correct, but locked
    assert not d["verified"] and s.verified_patient_id is None


def test_duplicate_names_resolved_by_dob():
    ex, s = make()
    call(ex, "verify_patient", full_name="robert smith", date_of_birth="1991-06-09")
    assert s.verified_patient_id == "P1005"
    out, _ = call(ex, "get_my_appointments")
    assert out["appointments"] == []  # must not see the other Robert Smith's cardiology visit


def test_cannot_touch_other_patients_appointment():
    ex, _ = make()
    call(ex, "verify_patient", full_name="David Chen", date_of_birth="1972-11-02")
    out, err = call(ex, "cancel_appointment", appointment_id="A-5004")  # Linda's
    assert err and out["error"]["code"] == "NOT_FOUND"
    assert ex.clinic.appointments["A-5004"].status == "booked"


def test_late_cancel_flagged():
    ex, _ = make()
    call(ex, "verify_patient", full_name="David Chen", date_of_birth="1972-11-02")
    out, _ = call(ex, "cancel_appointment", appointment_id="A-5002")
    assert out["late_cancellation"] is True


def test_search_book_and_slot_race():
    ex, _ = make(open_dates=[["RAO", "2026-10-15"]], faults={"book_appointment": {"steal_first_slot": True}})
    call(ex, "verify_patient", full_name="Hannah Kim", date_of_birth="2001-12-03")
    out, _ = call(ex, "search_availability", specialty="family_medicine", date_from="2026-10-15",
                  date_to="2026-10-15", time_of_day="afternoon")
    first, second = out["slots"][0]["slot_id"], out["slots"][1]["slot_id"]
    o1, e1 = call(ex, "book_appointment", slot_id=first, visit_type="follow_up", reason="checkup")
    assert e1 and o1["error"]["code"] == "SLOT_UNAVAILABLE"
    o2, e2 = call(ex, "book_appointment", slot_id=second, visit_type="follow_up", reason="checkup")
    assert not e2 and o2["booked"]


def test_transient_failure_and_auto_retry_guardrail():
    faults = {"search_availability": {"fail_times": 2, "code": "TIMEOUT"}}
    ex, _ = make(faults=faults)
    out, err = call(ex, "search_availability", specialty="dermatology", date_from="2026-10-19", date_to="2026-10-26")
    assert err and out["error"]["retryable"]
    ex2, _ = make(guards={"auto_retry_transient": True}, faults=faults)
    out2, err2 = call(ex2, "search_availability", specialty="dermatology", date_from="2026-10-19", date_to="2026-10-26")
    assert not err2 and ex2.calls[-1]["attempts"] == 3


def test_duplicate_booking_guardrail():
    for on in (False, True):
        ex, _ = make(guards={"block_duplicate_specialty_booking": on}, open_dates=[["RAO", "2026-10-20"]])
        call(ex, "verify_patient", full_name="Priya Patel", date_of_birth="1990-07-21")  # has A-5005 family med
        slots, _ = call(ex, "search_availability", specialty="family_medicine", date_from="2026-10-20", date_to="2026-10-20")
        out, err = call(ex, "book_appointment", slot_id=slots["slots"][0]["slot_id"], visit_type="follow_up", reason="x")
        assert err == on
        if on:
            assert out["error"]["code"] == "DUPLICATE_BOOKING"


def test_referral_required_and_unsupported_specialty():
    ex, _ = make()
    call(ex, "verify_patient", full_name="Aisha Johnson", date_of_birth="1988-09-05")
    slots, _ = call(ex, "search_availability", specialty="cardiology", date_from="2026-10-13", date_to="2026-11-30")
    out, err = call(ex, "book_appointment", slot_id=slots["slots"][0]["slot_id"], visit_type="consultation", reason="x")
    assert err and out["error"]["code"] == "REFERRAL_REQUIRED"
    out, err = call(ex, "search_availability", specialty="other", date_from="2026-10-13", date_to="2026-10-20")
    assert err and out["error"]["code"] == "UNSUPPORTED_SPECIALTY"


def test_reschedule_is_atomic_on_taken_slot():
    ex, _ = make()
    call(ex, "verify_patient", full_name="Maria Gonzalez", date_of_birth="1985-03-14")
    taken = next(iter(ex.clinic._taken))
    out, err = call(ex, "reschedule_appointment", appointment_id="A-5001", new_slot_id=taken)
    assert err and ex.clinic.appointments["A-5001"].start.day == 14


def test_red_flags():
    assert red_flags("crushing pressure in my chest and my arm is numb")
    assert not red_flags("I'd like a checkup on Thursday")


def test_search_says_when_results_are_truncated():
    ex, _ = make()
    out, _ = call(ex, "search_availability", specialty="family_medicine", date_from="2026-10-13", date_to="2026-10-30")
    assert len(out["slots"]) == 8 and out["total_open"] > 8 and "not shown" in out["note"]
    out, _ = call(ex, "search_availability", specialty="cardiology", date_from="2026-10-15", date_to="2026-10-15")
    assert out["total_open"] == len(out["slots"]) <= 8 and "all the open slots" in out["note"]


def test_verification_reports_new_patient_status():
    ex, _ = make()
    out, _ = call(ex, "verify_patient", full_name="Elena Novak", date_of_birth="1995-08-17")
    assert out["verified"] and out["new_patient"] is True
    ex, _ = make()
    out, _ = call(ex, "verify_patient", full_name="Maria Gonzalez", date_of_birth="1985-03-14")
    assert out["new_patient"] is False
