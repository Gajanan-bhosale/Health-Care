"""Fresh, independent A/B of two policies under identical scenarios and seeds.

The loop accepts a candidate based on one run, which was *selected* for looking good, so its score
is optimistically biased. This re-runs both policies from scratch with N trials each."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from harness.report import _row, _summary_line
from harness.runner import ROOT, run_suite
from scheduler.policy import Policy


def main() -> None:
    from scheduler.llm import USAGE, AnthropicLLM
    ap = argparse.ArgumentParser()
    ap.add_argument("before")
    ap.add_argument("after")
    ap.add_argument("--trials", type=int, default=2)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    out = Path(a.out or ROOT / "runs" / f"compare_{time.strftime('%Y%m%d_%H%M%S')}")
    llm = AnthropicLLM()
    pb, pa = Policy.load(a.before), Policy.load(a.after)
    rb = run_suite(llm, pb, out / f"before_v{pb.version}", a.trials)
    ra = run_suite(llm, pa, out / f"after_v{pa.version}", a.trials)
    L = [f"# Fresh A/B: policy v{pb.version} vs v{pa.version} ({a.trials} trials per scenario)", "",
         _summary_line(f"before v{pb.version}", rb["summary"]), _summary_line(f"after v{pa.version}", ra["summary"]), "",
         "| scenario | split | before | after | delta | | still failing (count over trials) |", "|---|---|---|---|---|---|---|"]
    L += [_row(sid, rb["scenarios"][sid], ra["scenarios"][sid]) for sid in rb["scenarios"]]
    (out / "COMPARE.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    (out / "usage.json").write_text(json.dumps(USAGE.snapshot(), indent=2), encoding="utf-8")
    print("\n".join(L))


if __name__ == "__main__":
    main()
