"""
Generates a batch of repo-repair tasks and writes them to
data/processed/tasks_v1.json -- this is the file we'll hand to `dvc add`
right after this, making it the first piece of data DVC actually tracks.

Run from the project root:
    python scripts/generate_tasks.py
"""

import json
from pathlib import Path

from src.domain.repo_repair.generator import RepoRepairDomain

OUTPUT_PATH = Path("data/processed/tasks_v1.json")
NUM_TASKS = 20  # small batch to start; easy to raise once this runs cleanly end-to-end


def main() -> None:
    domain = RepoRepairDomain()
    tasks = [domain.generate_task() for _ in range(NUM_TASKS)]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open("w") as f:
        json.dump([t.model_dump() for t in tasks], f, indent=2)

    breakdown: dict[str, int] = {}
    for t in tasks:
        key = f"{t.bug_family}:{t.variant}"
        breakdown[key] = breakdown.get(key, 0) + 1

    print(f"Wrote {len(tasks)} tasks to {OUTPUT_PATH}")
    print("Breakdown:", breakdown)
    print()
    print("Sample traceback (first task):")
    print(tasks[0].traceback)


if __name__ == "__main__":
    main()
