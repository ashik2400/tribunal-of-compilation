"""
Generates a batch of repo-repair tasks and writes them to
data/processed/tasks_v2.json (tracked by DVC).

Deterministic: task i uses seed i, and variants alternate so the set is
balanced, so rerunning this produces a byte-identical file on any machine.
tasks_v1.json (random variants, uuid ids, machine-specific traceback paths)
is kept unchanged for reference.

Run from the project root:
    python -m scripts.generate_tasks [--n 20] [--out data/processed/tasks_v2.json]
"""

import argparse
import json
from pathlib import Path

from src.domain.repo_repair.generator import VARIANTS, RepoRepairDomain


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--out", default="data/processed/tasks_v2.json")
    args = ap.parse_args()
    output_path = Path(args.out)

    domain = RepoRepairDomain()
    tasks = [domain.generate_task(seed=i, variant=VARIANTS[i % len(VARIANTS)]) for i in range(args.n)]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="\n") as f:
        json.dump([t.model_dump() for t in tasks], f, indent=2)
        f.write("\n")

    breakdown: dict[str, int] = {}
    for t in tasks:
        key = f"{t.bug_family}:{t.variant}"
        breakdown[key] = breakdown.get(key, 0) + 1

    print(f"Wrote {len(tasks)} tasks to {output_path}")
    print("Breakdown:", breakdown)
    print()
    print("Sample traceback (first task):")
    print(tasks[0].traceback)


if __name__ == "__main__":
    main()
