"""Find streaks of consistent solves in the Solver's log and draft rules from them.
    python -m scripts.compile_rules [--n 3]
Reads runs/solver_runs.jsonl, writes runs/rule_drafts.jsonl (drafts only: NOT trusted yet -
the Auditor and registry come next)."""
import argparse, json, os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
if not os.getenv("GROQ_API_KEY"):
    sys.exit("GROQ_API_KEY not found - check .env")

from src.compiler import Compiler, dedupe_candidates, find_candidates
from src.domain.repo_repair.solver_tools import validate_fix
from src.llm import make_llm

ap = argparse.ArgumentParser()
ap.add_argument("--n", type=int, default=3, help="consecutive consistent solves required")
ap.add_argument("--runs", default="runs/solver_runs.jsonl")
ap.add_argument("--out", default="runs/rule_drafts.jsonl")
args = ap.parse_args()

runs = [json.loads(l) for l in Path(args.runs).read_text().splitlines() if l.strip()]
if runs and "traceback" not in runs[0]:
    sys.exit("This log predates the Compiler (no traceback/files fields). Delete it and rerun run_solver.")
raw = find_candidates(runs, n_required=args.n)
cands = dedupe_candidates(raw)
print(f"{len(runs)} runs -> {len(raw)} streak(s) -> {len(cands)} distinct candidate rule(s)")

llm = make_llm({"provider": "groq", "model": os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")})
compiler = Compiler(llm, validate_action=validate_fix)
with open(args.out, "a") as f:
    for c in cands:
        rule, log = compiler.draft(c)
        print(f"\nsignature: {c.signature}\nfix template: {c.shape}  (from {c.n} solves)")
        if rule:
            print("scope:", [p.model_dump(exclude_none=True) for p in rule.scope] or "EMPTY (fires on anything with this signature)")
            print("rationale:", rule.rationale)
        else:
            print("draft failed:", log["errors"])
        f.write(json.dumps({"rule": rule.model_dump() if rule else None, "log": log}) + "\n")
print(f"\ndrafts appended to {args.out}")