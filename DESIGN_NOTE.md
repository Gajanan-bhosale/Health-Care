Design note: Patient-scheduling agent with a self-improving eval loop

1. Key design choices and why

• Stateless Prompt Security: Security limits are bound to the backend code rather than the prompt layer. Verification tools omit patient_id parameters entirely, pinning database read/write sessions to isolated, pre-authorized session context.
• Deterministic State Rendering: Session state variables (auth attempts, escalation tallies) are computed natively and dynamically re-injected into system instructions each turn, keeping context clean.
• Tool-Bound Knowledge Constraints: Financial, policy, and medical escalation trees are fetched via dynamic tool lookups, completely preventing model hallucination vectors.
• Cost-Efficient Compute Layering: Claude Haiku acts as the fast production model. Sonnet runs multi-layer transcript evaluation checks, and Opus synthesizes prompt optimization patches.
• Three-Tier Evaluation Rigor: Tests run via native database assertion blocks (state checks), regex pattern scanners (privacy/leak prevention), and a strict, evidence-quoting LLM judge.
• Zero-Tolerance Regression Gating: Any critical safety failure drops the run score directly to zero. Safe fallback behavior is heavily prioritized over conversational flair.

2. How the improvement loop works

• Multi-Persona Simulation: Automated tests run across 17 distinct scenarios (including a 4-case hidden holdout set) spanning prompt-injections, database outages, and mid-call medical crises.
• Isolated Patch Synthesis: Optimization models parse train-set failures to return micro-patches (playbook rule tweaks). Modifying base source files or tool boundaries is strictly blocked.
• Overfitting Lint Controls: System linters instantly drop any patch matching strict testing constants (e.g., specific fake names, addresses, or dates).
• Automated Rollback Gates: Patches are committed only if net performance spikes and zero historical baseline scenarios regress. Flaky trials are automatically re-run to reduce noise.
• Targeted Error Feedback: Dropped fixes are returned to the optimizer alongside precise failure traces, preventing repetitive patch iterations.
• Pre-Deployment A/B Verification: Final metrics are computed via clean, end-to-end A/B validation loops from scratch, isolating the numbers from loop selection bias.

3. Before and after scores

• Loop Phase (v0 → v2): Total passing test cases scaled from 9/16 to 14/16 (holdout moved from 2/4 to 3/4). Aggregate baseline scores shifted from 0.764 to 0.924.
• A/B Sweep (v0 vs v2): Clean passing configurations climbed from 10/16 to 13/16 (mean shift stayed inside noise variance thresholds: 0.937 vs 0.914).
• Final Iteration (v2 vs v3): Scenarios passing every trial went from 9/17 to 15/17 (7 fixed issues, 0 regressions), with mean scores climbing from 0.909 to 0.967.
• Safety Fixes: Resolved a severe baseline vulnerability where the agent issued unsupervised medication advice during cardiovascular emergencies.

4. One thing I'd change for a real clinic

• Outbound Grounding Guardrails: Build a synchronous validation wrapper between the agent's final text output stream and the active tool trace. The system would intercept and block any ungrounded dates, slot times, or medical numbers before they ever reach a patient screen.
• Production EHR Ecosystems: Transition data interactions from simulated environments to clinical sandboxes using standard HL7/FHIR microservices, enforce multi-factor identity protocols, and deploy human-in-the-loop review queues for escalations.

5. Where AI helped vs. where my judgment overrode it

• Where AI Helped: Writing boilerplate code structures, synthesizing complex user agent testing configurations, diagnosing log errors, and drafting tactical prompt patches.
• Where My Judgment Overrode It:
• Harness Bug Isolation: Caught the optimizer writing logic loops to bypass an ambiguous testing date constraint. Patched the core framework directly and introduced explicit harness verification logging.
• Patch Size Throttling: Restricted prompt optimization outputs to 3 changes per iteration, preventing bloated, multi-vector changes from breaking passing dependencies.
• Honest Data Reporting: Suppressed inflated optimization curve metrics, choosing to publish isolated, fresh A/B tests to remain statistically honest.
• Baseline Weakness Strategy: Deliberately omitted core validation rules from the v0 baseline prompt to ensure optimization metrics measured actual loop performance.
• Manual Fuzz Testing: Found 5 critical logic blind spots (e.g., hidden api result limits) via active hand-testing, routing fixes directly to data validation layers.
