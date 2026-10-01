"""Audit every drafted rule: generate twins, judge them, repair broken rules, record verdicts.
    python -m scripts.audit_rules [--k 3] [--mlflow]
Reads runs/rule_drafts.jsonl + runs/solver_runs.jsonl.
Writes runs/audit_reports.jsonl and appends breaking twins to tests/regression/twins.jsonl."""
import argparse, json, os, sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
if not os.getenv("GROQ_API_KEY"):
    sys.exit("GROQ_API_KEY not found - check .env")

from src.auditor import Auditor, RegressionSuite, candidate_from_rule
from src.compiler import Compiler
from src.domain.repo_repair.audit_backend import make_backend
from src.domain.repo_repair.solver_tools import validate_fix
from src.llm import make_llm
from src.rules import Rule
from src.tracking import log_audit

ap = argparse.ArgumentParser()
ap.add_argument("--k", type=int, default=3, help="Solver runs per twin")
ap.add_argument("--twins", type=int, default=5)
ap.add_argument("--gen-rounds", type=int, default=4)
ap.add_argument("--per-round", type=int, default=3, help="twins requested per LLM call")
ap.add_argument("--mutations", type=int, default=3, help="deterministic twins to evaluate (0 disables)")
ap.add_argument("--controls", type=int, default=3, help="benign control twins to evaluate (0 disables)")
ap.add_argument("--rule", default=None, help="audit only this rule_id (saves tokens)")
ap.add_argument("--drafts", default="runs/rule_drafts.jsonl")
ap.add_argument("--runs", default="runs/solver_runs.jsonl")
ap.add_argument("--out", default="runs/audit_reports.jsonl")
ap.add_argument("--regression", default="tests/regression/twins.jsonl")
ap.add_argument("--mlflow", action="store_true")
args = ap.parse_args()

runs = [json.loads(l) for l in Path(args.runs).read_text().splitlines() if l.strip()]
drafts = [json.loads(l) for l in Path(args.drafts).read_text().splitlines() if l.strip()]
rules, seen = [], set()
for d in drafts:                       # newest draft per rule id wins; skip failed drafts
    if d["rule"]:
        r = Rule.model_validate(d["rule"])
        if r.rule_id not in seen:
            seen.add(r.rule_id); rules.append(r)

if args.rule:
    rules = [r for r in rules if r.rule_id == args.rule]
    if not rules:
        sys.exit(f"no drafted rule with id {args.rule}")
llm = make_llm({"provider": "groq", "model": os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")})
auditor = Auditor(llm, make_backend(llm, mutations=args.mutations > 0, controls=args.controls > 0), k=args.k, n_twins=args.twins, gen_rounds=args.gen_rounds,
                  per_round=args.per_round, max_mutations=args.mutations,
                  max_controls=args.controls)
compiler = Compiler(llm, validate_action=validate_fix)
regression = RegressionSuite(args.regression)
Path(args.out).parent.mkdir(parents=True, exist_ok=True)

with open(args.out, "a") as f:
    for rule in rules:
        try:
            rep = auditor.run(rule, candidate_from_rule(rule, runs), compiler, regression)
        except Exception as e:
            print(f"\n=== {rule.rule_id}: audit CRASHED ({type(e).__name__}: {str(e)[:200]})")
            print("    Breaking twins found so far are saved in the regression suite; rerun to resume cheaply.")
            continue
        f.write(json.dumps(rep.to_dict()) + "\n"); f.flush()
        if args.mlflow and not log_audit(rep):
            print("(mlflow not installed: pip install mlflow)")
        print(f"\n=== {rule.rule_id}  action={rule.action['type']}  -> {rep.final_verdict.upper()}  "
              f"(repairs used: {rep.repairs_used})")
        for i, rd in enumerate(rep.rounds):
            print(f" round {i}: {rd.verdict} - {rd.reason}")
            print("   scope:", [p for p in rd.rule["scope"]] or "EMPTY")
            for t in rd.twins:
                print(f"   twin {t.verdict:12} [{t.source}] {sorted(t.files)} - {t.reason[:100]}")
        print(" final:", rep.reason)