"""Markdown report for a loop run: before/after per scenario, patches, gate decisions."""
from __future__ import annotations

from pathlib import Path


def _row(sid: str, b: dict, a: dict) -> str:
    def cell(s):
        return f"{'PASS' if s['pass_rate'] == 1 else 'FAIL'} {s['score']:.2f} ({s['pass_rate']:.0%})"
    delta = a["score"] - b["score"]
    flag = "fixed" if b["pass_rate"] < 1 <= a["pass_rate"] else ("REGRESSED" if b["pass_rate"] == 1 > a["pass_rate"] else "")
    failed = ", ".join(a["failed_checks"]) if a["failed_checks"] else ""
    return f"| {sid} | {b['split']} | {cell(b)} | {cell(a)} | {delta:+.2f} | {flag} | {failed} |"


def _summary_line(label: str, s: dict) -> str:
    parts = []
    for split in ("train", "holdout", "all"):
        if split in s:
            x = s[split]
            parts.append(f"{split}: score {x['mean_score']:.3f}, {x['fully_passing']}/{x['scenarios']} passing")
    return f"- **{label}** - " + "; ".join(parts)


def write_report(run_dir: Path, record: dict, final_policy) -> Path:
    base, final = record["baseline"]["result"], record["final"]["result"]
    L = ["# Self-improvement loop report", "",
         f"Baseline policy v{record['baseline']['policy']['version']} -> final policy v{final_policy.version}", "",
         "## Headline", _summary_line("before", base["summary"]), _summary_line("after", final["summary"]), ""]
    if "confirm" in record:
        L += ["Independent confirmation re-run (fresh trials, removes selection bias of the accepted run):",
              _summary_line("before (fresh)", record["confirm"]["baseline"]["summary"]),
              _summary_line("after (fresh)", record["confirm"]["final"]["summary"]), ""]
    L += ["## Per scenario (score, pass rate across trials)", "",
          "| scenario | split | before | after | delta | | still failing |", "|---|---|---|---|---|---|---|"]
    for sid in base["scenarios"]:
        L.append(_row(sid, base["scenarios"][sid], final["scenarios"][sid]))
    L += ["", "## Iterations", ""]
    for it in record["iterations"]:
        if it.get("status") == "lint_failed":
            L += [f"### Iteration {it['iteration']}: optimizer patch failed lint", "", *[f"- {e}" for e in it["errors"]], ""]
            continue
        d = it["decision"]
        L += [f"### Iteration {it['iteration']} -> candidate v{it['candidate_version']}: "
              f"**{'ACCEPTED' if d['accept'] else 'REJECTED'}** ({d['reason']})", "",
              f"- before: {d['prev']['all']}", f"- candidate: {d['cand']['all']}"]
        if d["flaky_rechecked"]:
            L.append(f"- suspected regressions that passed on recheck (flaky): {d['flaky_rechecked']}")
        L += ["", "Diagnosis:"]
        L += [f"- `{x['scenario']}` / {x['failed_check']}: "
              f"{'**[suspected harness issue - for human review]** ' if x.get('suspected_harness_issue') else ''}"
              f"{x['root_cause']}" for x in it["patch"]["diagnosis"]]
        L += ["", "Changes:"]
        for c in it["patch"]["changes"]:
            target = c.get("rule_id") or c.get("guardrail")
            L.append(f"- **{c['op']}** `{target}`" + (f": {c['text']}" if c.get("text") else ""))
            L.append(f"  - why: {c['rationale']} (addresses: {', '.join(c['addresses'])})")
        L.append("")
    L += ["## Final policy", "", "Guardrails on: " + (", ".join(k for k, v in final_policy.guardrails.items() if v) or "none"), "",
          "Playbook rules:"]
    L += [f"- `{r.id}` (v{r.added_in}): {r.text}" for r in final_policy.rules] or ["- (none)"]
    L += ["", f"Raw data: `loop.json`; transcripts per iteration in `iter*_v*/` (baseline `{record['baseline']['dir']}/`, final `{record['final']['dir']}/`)."]
    out = run_dir / "REPORT.md"
    out.write_text("\n".join(L) + "\n", encoding="utf-8")
    return out
