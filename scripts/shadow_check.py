"""Sample the Router's rule-handled decisions, re-run them through the Solver, demote rules that diverge.
    python -m scripts.shadow_check [--rate 0.2] [--max-checks 6]
Only sampled decisions cost LLM tokens. Reads runs/router_log.jsonl, appends to runs/shadow_log.jsonl."""
import argparse, json, os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
from src.auditor import RegressionSuite
from src.domain.repo_repair.audit_backend import decision_key, make_backend
from src.registry import Registry
from src.shadow import ShadowChecker

ap = argparse.ArgumentParser()
ap.add_argument("--registry", default="rules/registry.json")
ap.add_argument("--router-log", default="runs/router_log.jsonl")
ap.add_argument("--shadow-log", default="runs/shadow_log.jsonl")
ap.add_argument("--regression", default="tests/regression/twins.jsonl")
ap.add_argument("--rate", type=float, default=0.2, help="fraction of rule-handled decisions to re-check")
ap.add_argument("--k", type=int, default=3, help="Solver runs per check")
ap.add_argument("--demote-after", type=int, default=1, help="confirmed divergences that trigger demotion")
ap.add_argument("--max-checks", type=int, default=6, help="cap per invocation (protects your token budget)")
ap.add_argument("--seed", type=int, default=0)
args = ap.parse_args()

records = [json.loads(l) for l in Path(args.router_log).read_text().splitlines() if l.strip()]
reg = Registry(args.registry, same_decision=lambda a, b: decision_key(a) == decision_key(b))
state = {"fn": None}
def solve(task):                                   # the Solver (and the Groq key) is only needed if something is sampled
    if state["fn"] is None:
        if not os.getenv("GROQ_API_KEY"):
            sys.exit("GROQ_API_KEY not found - check .env")
        from src.domain.repo_repair.solver_factory import default_solver_factory
        fn = default_solver_factory()
        state["fn"] = lambda t: fn(t)[:2]
    return state["fn"](task)

checker = ShadowChecker(reg, make_backend(solve_fn=solve), RegressionSuite(args.regression), k=args.k,
                        sample_rate=args.rate, demote_after=args.demote_after, seed=args.seed,
                        max_checks=args.max_checks, log_path=args.shadow_log)
results = checker.run(records)
print(f"{len(results)} decision(s) shadow-checked of {sum(r['handled_by'] == 'rule' for r in records)} rule-handled\n")
for r in results:
    print(f"{r.task_id[-24:]:26} {r.rule_id:14} {r.outcome:12} {'DEMOTED' if r.demoted else ''} {r.reason[:60]}")
print(f"\n{'rule':16} {'state':11} {'checks':>6} {'diverged':>8} {'inconclusive':>12}")
for rid, e in reg.entries.items():
    s = e.stats
    print(f"{rid:16} {e.state:11} {s.get('shadow_checks', 0):6} {s.get('divergences', 0):8} {s.get('inconclusive', 0):12}")
