"""CI gate: fail (exit 1) if any production rule is unsafe. No LLM, no API key needed.
    python -m scripts.check_registry"""
import argparse, sys
from pathlib import Path

from src.auditor import RegressionSuite
from src.ci_checks import check_registry
from src.domain.repo_repair.audit_backend import decision_key, make_backend
from src.registry import Registry

ap = argparse.ArgumentParser()
ap.add_argument("--registry", default="rules/registry.json")
ap.add_argument("--regression", default="tests/regression/twins.jsonl")
args = ap.parse_args()

if not Path(args.registry).exists():
    print(f"no registry at {args.registry}: nothing to check")
    sys.exit(0)
reg = Registry(args.registry, same_decision=lambda a, b: decision_key(a) == decision_key(b))
suite = RegressionSuite(args.regression)
prod = [r for r in reg.entries.values() if r.state == "production"]
print(f"{len(reg.entries)} rule(s), {len(prod)} in production, {len(suite.records)} stored twin(s)")
problems = check_registry(reg, suite, make_backend())
for p in problems:
    print("FAIL:", p)
print("registry OK" if not problems else f"{len(problems)} problem(s)")
sys.exit(1 if problems else 0)
