# Design note: Patient-scheduling agent with a self-improving eval loop

## Key design choices and why

- **Safety is enforced in code, not in the prompt.** No tool takes a `patient_id`. `verify_patient`
  binds the session to one patient, and every later read or write is scoped to that patient, so a
  caller cannot talk the model into touching someone else's record. Verification gives the same answer
  for "wrong DOB" and "no such person", and it locks after 3 failures. Errors are typed
  (`{code, message, retryable}`) so the agent branches on codes, not prose.
- **Session state is owned by code.** Who is verified, how many attempts failed and which escalations
  were filed are re-rendered into the system prompt on every call. The model never has to work this out
  from a long transcript.
- **Fees, rules and emergencies are answered from tools.** `get_clinic_policy` and `escalate_to_human`
  exist so the agent never answers these from model memory.
- **The cheapest model is the agent.** Haiku runs the calls, Sonnet judges and Opus optimizes. The
  agent is what you would deploy at scale, and the loop's job is to make a cheap model reliable.
- **Scoring has three layers, because a transcript-only judge can't see everything.**
  1. Deterministic DB checks catch double bookings, a "you're booked" when the tool actually failed,
     and writes to the wrong record.
  2. Regex checks catch another patient's details being disclosed.
  3. An LLM judge scores the language-level criteria (confirmed before committing, grounded claims,
     no medical advice), sees the tool trace and must quote verbatim evidence.

  Any critical failure scores the trial zero, because a safe-but-clumsy call beats a polished unsafe one.

## How the improvement loop works

1. Run 17 scenarios (13 train, 4 holdout) with simulated patients who follow scripted "beats", so hard
   moments really happen: chest pain mid-call, a slot stolen before booking, a prompt-injection attempt.
2. Send only the **train** failures to the optimizer. It returns a typed patch: a root-cause diagnosis
   plus 1–3 playbook-rule or guardrail changes, each tied to the `scenario:check` it fixes. It cannot
   edit the base prompt or the tools.
3. **Lint** the patch. Rules that name specific patients, DOBs or IDs are rejected as memorised test
   cases.
4. Re-run **all** scenarios. **Gate:** accept the patch only if the score improves _and_ no previously
   passing scenario regresses (holdout included). Suspected regressions are re-run once to separate
   flakes from real failures.
5. Return rejected patches to the optimizer with the reason they failed, so it doesn't repeat them.
6. **Fresh A/B:** re-run the old and new policies from scratch. The accepted run was selected for
   looking good, so its numbers are optimistically biased.

## Before and after scores

| step                | scenarios passing every trial           | mean score                    |
| ------------------- | --------------------------------------- | ----------------------------- |
| Loop: v0 → v2       | 9/16 → **14/16** (holdout 2/4 → 3/4)    | 0.764 → **0.924**             |
| Fresh A/B: v0 vs v2 | 10/16 → **13/16**                       | 0.937 vs 0.914 (within noise) |
| Fresh A/B: v2 vs v3 | 9/17 → **15/17** (7 fixed, 0 regressed) | 0.909 → **0.967**             |

The most important fix: baseline v0 told a possible heart-attack caller to "chew aspirin". The learned
`emergency_no_advice` rule fixed it. Two scenarios still fail: borderline dosing remarks under pressure
and an occasional confirmation lapse after a slot is taken.

## One thing I'd change for a real clinic

I'd add a **code-level grounding guardrail before anything reaches the patient.** Every date, time,
provider and fee the agent says would be checked against what the tools actually returned in that
session, and any mismatch would be blocked or corrected. The remaining failures (misread slot times,
borderline advice) are exactly the kind that prompt rules reduce but can't guarantee against. In a
clinic, one wrong appointment time can mean a missed visit. Alongside this, I'd connect to the real
EHR/scheduling system (e.g., FHIR), use stronger identity checks than name + DOB, and have staff
review escalations.

## Where AI helped vs. where my judgment overrode it

**AI helped:** scaffolding the code, drafting scenarios and personas, and, inside the loop itself,
diagnosing failures and proposing patches. That is the point of the system.

**Where my judgment overrode it:**

- **The optimizer patched around my bugs.** The first loop run did not close. The optimizer invented a
  "new-patient lead time" policy to explain a failure. Reading the transcripts showed the real cause
  was a **mislabeled check** ("next Wednesday" was ambiguous). I fixed the harness, not the agent, and
  added a `suspected_harness_issue` flag. The optimizer later used it correctly to report three real
  harness bugs.
- **Patch size.** Five-change patches meant one bad rule sank four good ones. I capped patches at 3
  changes and made rejection feedback explain _why_ a patch failed.
- **Not trusting the headline number.** The gate's accepted score looked great, but the fresh A/B showed
  v2's mean-score gain was within noise. I report that honestly rather than the inflated loop number.
- **A deliberately weak base prompt.** I didn't hand-write rules for confirmations, emergencies or late
  fees. The loop had to discover them, which is the only way to show it actually works.
- **Hand-testing.** Chatting with the agent myself found 5 gaps the eval missed, such as the slot search
  silently capping at 8 results and filler appointment reasons. Each was fixed at the layer where it
  belonged: tool, data or eval.

_Full details and evidence are in `README.md`, `RESULTS.md` and `results/`._
