# Design note: Patient-scheduling agent with a self-improving eval loop

A Claude-based phone-scheduling agent for a fictional clinic, a scenario harness that scores it on hard
cases, and a loop that turns failed calls into small policy patches. A patch is kept only if the score
goes up **and** nothing that already worked breaks.

## Key design choices and why

- **Safety is enforced in code, not in the prompt.** No tool takes a `patient_id`. `verify_patient`
  binds the session to one patient, and every later read or write is scoped to that patient, so a
  caller cannot talk the model into touching someone else's record. A prompt can only ask for this;
  code guarantees it. Verification gives the same answer for "wrong DOB" and "no such person" (no way
  to probe who is a patient) and locks after 3 failures.
- **Session state is owned by code.** Who is verified, failed attempts and escalations are
  re-rendered into the system prompt on every call, so the model never has to work this out from a
  long transcript.
- **Facts come from tools, not model memory.** Fees, referral rules and emergencies go through
  `get_clinic_policy` and `escalate_to_human`. Errors are typed (`{code, message, retryable}`) so the
  agent branches on codes, not prose.
- **The cheapest model is the agent.** Haiku runs the calls, Sonnet judges and Opus optimizes. The agent
  is what would run at scale, so the loop's job is to make a cheap model reliable.
- **Scoring has three layers, because a judge that only reads the transcript can't see whether things
  actually happened.**
  1. Deterministic DB checks catch double bookings, a "you're booked" when the tool actually failed,
     and writes to the wrong patient's record.
  2. Regex checks catch another patient's appointment details being disclosed.
  3. An LLM judge scores language-level criteria (confirmed before committing, claims grounded in tool
     results, no medical advice). It sees the tool trace, and its verdicts must quote the transcript
     verbatim, which the harness then verifies.

  Any critical failure (unsafe advice, a privacy leak, an ungrounded claim) scores the trial zero,
  because a safe-but-clumsy call beats a polished unsafe one.

## How the improvement loop works

1. **Simulate.** Run 17 scenarios (13 train, 4 holdout) against an LLM patient that follows scripted
   "beats", so the hard moments really happen: chest pain mid-call, a slot taken by someone else before
   booking, a prompt-injection "admin override".
2. **Diagnose and patch.** Only **train** failures go to the optimizer; it never sees holdout
   transcripts. It returns a typed patch: a root-cause diagnosis plus 1–3 playbook-rule or guardrail
   changes, each tied to the `scenario:check` it fixes. It cannot edit the base prompt or the tools.
3. **Lint.** Rules that mention specific patients, DOBs or appointment IDs are rejected, because they
   memorise a test case instead of fixing the behaviour.
4. **Gate.** Re-run **all** scenarios. Accept the patch only if the score improves _and_ no previously
   passing scenario regresses, holdout included. A suspected regression is re-run once to tell flakes
   from real failures.
5. **Learn from rejection.** A rejected patch is shown to the optimizer next iteration with the
   regressed checks and why they failed, so it doesn't repeat the mistake.
6. **Confirm with a fresh A/B.** The accepted run was selected for looking good, so its numbers are
   biased upward. Old and new policies are re-run from scratch with identical seeds before I claim an
   improvement.

## Before and after scores

| step | scenarios passing every trial | mean score |
| --- | --- | --- |
| Loop run: v0 → v2 | 9/16 → **14/16** (holdout 2/4 → 3/4) | 0.764 → **0.924** |
| Fresh A/B: v0 vs v2 | 10/16 → **13/16** | 0.937 → 0.914 (slight drop, within noise) |
| Fresh A/B: v2 vs v3 | 9/17 → **15/17** (7 fixed, 0 regressed) | 0.909 → **0.967** |

- In the v0 → v2 run, one patch was rejected for breaking a scenario that already passed. The optimizer
  revised it from the rejection feedback, and the revision was accepted.
- The v2 vs v3 comparison is a harder test: a 17th scenario had been added, the checks were stricter
  (visit type and a real reason required), and three harness bugs were fixed. That is why v2 scores
  lower there than in its own loop run.
- **Most important fix:** baseline v0 told a possible heart-attack caller to "chew aspirin". The learned
  `emergency_no_advice` rule fixed it: 911, escalate, no medication or diagnosis.
- **Still failing:** borderline dosing remarks when a caller pushes for advice ("don't take more than
  the label says"), and an occasional confirmation lapse after a slot is taken by someone else.

## One thing I'd change for a real clinic

**A code-level grounding guardrail on every outgoing message.** Before anything reaches the patient,
every date, time, provider and fee the agent states would be checked against what the tools actually
returned in that session. Any mismatch would be blocked and regenerated.

Why this one: the remaining failures (a misread slot time, borderline advice) are exactly the kind
that prompt rules reduce but can't guarantee against, and in a clinic one wrong appointment time means
a missed visit. It applies the same principle as patient scoping: move guarantees out of the prompt and
into code. Beyond that, production would also need the real EHR/scheduling system (e.g. via FHIR),
stronger identity checks than name + DOB, and staff review of escalations.

## Where AI helped vs. where my judgment overrode it

**AI helped:** scaffolding the code, drafting scenarios and patient personas, and, inside the loop
itself, diagnosing failed conversations and proposing rule patches. That last part is the point of the
system: the optimizer found and fixed the emergency, late-fee and confirmation gaps without me writing
those rules.

**Where my judgment overrode it:**

- **The optimizer patched around my bugs.** The first loop run did not close. To explain a failure, the
  optimizer invented a "new-patient lead time" policy that exists nowhere in the clinic. Reading the
  transcripts showed the real cause was a **mislabeled check**: "next Wednesday", said on a Monday, is
  ambiguous. I fixed the harness, not the agent, and added a `suspected_harness_issue` flag. The
  optimizer later used that flag to report three real harness bugs.
- **Smaller patches.** With five-change patches, one bad rule sank four good ones. I capped patches at
  3 changes and made rejection feedback say _why_ a patch failed, not just that it did.
- **Not trusting the headline number.** The gate's accepted run looked great, but the fresh A/B showed
  v2's mean score didn't actually improve. Only the pass count did. I report the honest number, and
  I made the fresh A/B a standard step.
- **A deliberately weak base prompt.** I didn't hand-write rules for confirmations, emergencies or late
  fees. The loop had to discover them, which is the only real evidence that it works.
- **Testing by hand.** Chatting with the agent myself found 5 gaps the eval missed, such as slot search
  silently capping at 8 results and filler appointment reasons ("patient requested visit"). I fixed each
  at the layer it belonged to: tool, data, eval or loop.

_Full details and evidence: `README.md`, `RESULTS.md` and `results/`._
