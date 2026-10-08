"""Stream every task through the Router and measure what the rules actually bought you.
    python -m scripts.run_router
Uses production rules from rules/registry.json. The Solver (Groq) is only called for tasks no rule handles,
so if every task is covered this makes ZERO LLM calls."""
import argparse, json, os, statistics
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
from src.domain.repo_repair.audit_backend import decision_key
from src.domain.repo_repair.schema import RepoRepairTask
from src.domain.repo_repair.solver_tools import is_correct, verify_fix_explained
from src.registry import Registry
from src.router import Router
from src.domain.repo_repair.solver_factory import default_solver_factory

ap = argparse.ArgumentParser()
ap.add_argument("--tasks", default="data/processed/tasks_v1.json")
ap.add_argument("--registry", default="rules/registry.json")
ap.add_argument("--solver-log", default="runs/solver_runs.jsonl", help="used to estimate the all-Solver baseline")
ap.add_argument("--log", default="runs/router_log.jsonl")
args = ap.parse_args()

tasks = [RepoRepairTask(**d) for d in json.loads(Path(args.tasks).read_text())]
reg = Registry(args.registry, same_decision=lambda a, b: decision_key(a) == decision_key(b))
print(f"{len(reg.production_rules())} production rule(s), {len(tasks)} tasks\n")

solver = {"fn": None}
def solver_fn(task):
    if solver["fn"] is None:
        solver["fn"] = default_solver_factory()
    return solver["fn"](task)

router = Router(reg, solver_fn, verify=verify_fix_explained, log_path=args.log)
rows = []
for t in tasks:
    r = router.route(t)
    rows.append((t, r, is_correct(t, r.fix)))
    print(f"{t.variant:20} -> {r.handled_by:6} {r.rule_id or '':14} correct={rows[-1][2]!s:5} llm_calls={r.llm_calls}")

n = len(rows)
by_rule = [x for x in rows if x[1].handled_by == "rule"]
wrong_rule = [x for x in by_rule if not x[2]]
wrong_all = [x for x in rows if not x[2]]
calls = sum(r.llm_calls for _, r, _ in rows)
base = None
if Path(args.solver_log).exists():
    steps = [json.loads(l)["steps"] for l in Path(args.solver_log).read_text().splitlines() if l.strip()]
    if steps:
        base = statistics.mean(steps) * n
print(f"\nhandled by rule : {len(by_rule)}/{n}  ({len(by_rule)/n:.0%} without any LLM call)")
print(f"wrong fixes     : {len(wrong_all)}/{n} overall, {len(wrong_rule)}/{len(by_rule) or 1} among rule-handled "
      f"(false-compilation rate {len(wrong_rule)/len(by_rule) if by_rule else 0:.1%})")
print(f"LLM calls       : {calls}" + (f"  vs ~{base:.0f} if every task went to the Solver "
                                      f"({1 - calls/base:.0%} saved)" if base else ""))
print(f"decisions logged to {args.log}")
