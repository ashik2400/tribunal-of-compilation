"""Run the Solver on real repo_repair tasks and log every run as JSON lines.
    python -m scripts.run_solver --n 6
Reads data/processed/tasks_v1.json (generates it first: python scripts/generate_tasks.py).
Output: runs/solver_runs.jsonl  (input for the Compiler in the next milestone)."""
import argparse, collections, json, os, sys, time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
if not os.getenv("GROQ_API_KEY"):
    sys.exit("GROQ_API_KEY not found - check .env")

from src.domain.repo_repair.generator import RepoRepairDomain
from src.domain.repo_repair.schema import RepoRepairTask
from src.domain.repo_repair.solver_tools import (INSTRUCTIONS, RepoSandbox, is_correct,
                                                 render_task, validate_fix, verify_fix_explained)
from src.llm import make_llm
from src.solver import Solver

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=6)
ap.add_argument("--tasks", default="data/processed/tasks_v1.json")
ap.add_argument("--out", default="runs/solver_runs.jsonl")
args = ap.parse_args()

tasks = [RepoRepairTask(**d) for d in json.loads(Path(args.tasks).read_text())][: args.n]
llm = make_llm({"provider": "groq", "model": os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")})
domain = RepoRepairDomain()
Path(args.out).parent.mkdir(parents=True, exist_ok=True)
tally = collections.defaultdict(lambda: collections.Counter())

with open(args.out, "a") as f:
    for task in tasks:
        sandbox = RepoSandbox(task)
        try:
            solver = Solver(llm, sandbox.tools(), verify_fix_explained, instructions=INSTRUCTIONS,
                            render_task=render_task, validate=validate_fix)
            res = solver.solve(task)
        finally:
            sandbox.close()
        correct = is_correct(task, res.solution)
        strict = isinstance(res.solution, dict) and domain.check_ground_truth(task, res.solution)
        rec = {"ts": time.time(), "task_id": task.task_id, "bug_family": task.bug_family,
               "variant": task.variant, "solution": res.solution, "verified": res.verified,
               "correct": correct, "strict_match": strict, "confidence": res.confidence,
               "self_reported_confidence": res.self_reported_confidence, "attempts": res.attempts,
               "steps": res.steps, "reflections": res.reflections, "trace": res.trace}
        f.write(json.dumps(rec) + "\n"); f.flush()
        t = tally[task.variant]; t["n"] += 1; t["verified"] += res.verified; t["correct"] += correct
        print(f"{task.variant:20} verified={res.verified!s:5} correct={correct!s:5} "
              f"conf={res.confidence} attempts={res.attempts} steps={res.steps}")

print("\nvariant                n  verified  correct")
for v, t in tally.items():
    print(f"{v:20} {t['n']:3} {t['verified']:8} {t['correct']:8}")
print(f"\nfull traces in {args.out}")