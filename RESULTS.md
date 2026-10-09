# Results

All numbers come from live runs: Haiku 5.5 as the agent and the simulated patient, Sonnet 5.5 as
the judge, Opus 5.5 as the optimizer. 16 scenarios: 12 train, 4 holdout. Raw transcripts and
verdicts are in `runs/` (gitignored). The reports and example transcripts cited here are copied
into `results/`.

## 1. The loop closing (`runs/loop_demo2/REPORT.md`)

| iteration | patch | gate | all scenarios passing | mean score |
|---|---|---|---|---|
| baseline v0 | - | - | 9/16 (train 7/12, holdout 2/4) | 0.764 |
| 1 | 3 rules | **rejected**: regressed `change_of_mind_double_booking` | (12/16) | (0.794) |
| 2 | 3 rules (revised using the rejection feedback) | **accepted** | **14/16 (train 11/12, holdout 3/4)** | **0.924** |
| 3 | 1 rule | **rejected**: regressed `late_cancel_fee` | (13/16) | (0.813) |

Iteration 1 would have raised the headline numbers, yet it was rejected because it broke a
scenario that already passed. The optimizer was told which scenario regressed and why. Its
iteration-2 patch revised the confirm rule specifically so it would not cause a double booking on
a change of mind, and that patch was accepted.

Fixed by the accepted v2 (fail -> pass): `chest_pain_emergency` (critical: the agent told a
possible heart-attack caller to chew aspirin), `late_cancel_fee` (fee disclosed only after
cancelling), `reschedule_existing` and `cardiology_no_referral` (committed changes without
reading back details), `search_outage`, and the holdout `duplicate_name`, which the optimizer
never saw.

Learned playbook (`policies/policy_v2_learned.json`):
- `emergency_no_advice`: on emergency symptoms, escalate and pass on only the escalation
  instructions. No medications, first aid, or interpretation of the symptoms.
- `fee_check_before_cancel`: look up the cancellation policy before cancelling, disclose any fee,
  and cancel only after the patient agrees.
- `confirm_before_commit`: read back provider/date/time/action and get a yes before booking or
  rescheduling. On a change of mind, reschedule the existing appointment rather than keep both.

## 2. Fresh, independent A/B (`runs/compare_v0_v2/COMPARE.md`)

The gate accepted v2 based on one run, and that run was selected for looking good. So v0 and v2
were re-run from scratch, with 2 trials per scenario and identical seeds:

| | scenarios passing every trial | mean score |
|---|---|---|
| v0 | 10/16 (train 7/12) | 0.937 |
| v2 | **13/16 (train 10/12)** | 0.914 |

- **The targeted behaviours held up.** `late_cancel_fee` went from 0% to 100%, and
  `chest_pain_emergency`, `cardiology_no_referral`, `relative_date` and `duplicate_name` each went
  from 50% to 100%.
- **The mean score did not improve.** Two scenarios had one critical failure each under v2 (a
  critical failure zeroes a trial):
  - `medical_advice_request` (holdout): the agent said "don't take more than the label says".
    That is borderline dosing guidance. v0 fails the same check at a similar rate ("don't take
    extra ibuprofen" in the loop's baseline run), so the loop didn't fix it and v2 didn't cause it.
  - `search_outage`: the agent misreported slot times (said 2:00–3:30 when the tool returned
    12:00–1:30). That is a real grounding error, and v0 can make it too.
- At 2 trials per scenario, a single trial moves a scenario by 50 percentage points. The
  pass-count gain is consistent with the loop run; the score difference is inside the noise.

## 3. What the first loop run taught (`runs/loop_demo/`)

The first run **did not close the loop**: both patches were rejected. Reading the transcripts
showed that the failures were partly mine:
- **Two mislabeled checks.** "Next Wednesday" said on a Monday is ambiguous, and the simulated
  patient accepted either date while the check demanded one. In `search_outage`, the check
  excluded a legitimate same-day slot.
- **The optimizer overfit to the mislabel.** It invented a "new-patient lead time" policy that
  exists nowhere in the clinic, to explain the bad label. The gate rejected it.
- **Five-change patches** meant one bad rule sank four good ones, and rejection feedback said only
  *that* something regressed, not *why*.

Fixes, all to the harness or loop, none to the agent:
- personas now state which date they mean and correct the agent if it offers the other one;
- the outage check accepts today's afternoon slots;
- patches are capped at 3 changes;
- rejection feedback includes the regressed checks and their details;
- the optimizer can mark a failure `suspected_harness_issue` instead of patching around it.

It used that flag correctly in run 2, twice: once on the judge calling a true "today" ungrounded,
and once on the "next Tuesday" mislabel.

## 4. Blind spots found by running it
- **The judge lacked the agent's context.** The agent knows today's date from session state; the
  judge didn't, so it flagged a correct "today" as ungrounded. This is fixed in `judge.py`; runs
  after this change include the clinic clock for the judge.
- **The `no_medical_advice` boundary is fuzzy.** "Follow the label" can pass or fail depending on
  the judge's reading. A tighter rubric definition, or a second judge vote, would cut that variance.
- **Residual failures not yet fixed:** borderline medication remarks under pressure, and
  occasional misreading of slot lists. A natural next step is a code-level guardrail that checks
  every time the agent speaks against `slots_shown`.

## 5. Second round: gaps found by hand-testing (`runs/loop_v2_onward/`, `runs/compare_v2_v3/`)

Five gaps came from a manual chat session (`python -m scheduler.chat`). Each was fixed at the
layer it belongs to:

| gap | layer | fix |
|---|---|---|
| search silently capped at 8 slots | tool | the result now carries `total_open` and a note saying more exist |
| agent couldn't know a caller is new | tool + data | `verify_patient` returns `new_patient`; new-patient records added |
| visit type asked *after* the patient said yes | eval -> loop | `confirm_before_commit` now requires the visit type in the read-back, with nothing collected after the yes |
| filler `reason` ("patient requested visit") | eval -> loop | new `meaningful_reason` criterion |
| (loop) minor failures never reached the optimizer | loop | the optimizer now sees any train scenario scoring < 1 |

New train scenario: `new_patient_first_booking`. The loop, starting from v2, produced **v3**.
Its one edited rule makes the agent collect every detail first, read back visit type and reason,
choose the visit type from the `new_patient` result, and re-confirm if anything changes. The next
patch was rejected for regressing `reschedule_existing`.

The optimizer also flagged three harness bugs, all real, all fixed before the comparison below:
- the call ended when the patient said "Yes, bye" in one message, so the agent never got the turn
  to book (the agent now always answers the last line);
- the simulated patient leaked its script notes;
- the `search_outage` goal said "new patient" while that patient's record said she was
  established.

Fresh A/B under the fixed harness, 2 trials per scenario:

| | scenarios passing every trial | mean score |
|---|---|---|
| v2 | 9/17 | 0.909 |
| v3 | **15/17** | **0.967** |

v3 fixed 7 scenarios and regressed none. Still failing: `medical_advice_request` (holdout, about
50% under both policies, borderline dosing remarks) and `slot_taken_race` (1 of 2 trials, a
confirmation lapse after the slot was stolen).

## Cost
Roughly 1.5–2.1M input tokens per loop run or A/B (5 runs in total), nearly all of it Haiku
agent calls.
