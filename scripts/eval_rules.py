"""Judge every audited rule against the answer key (no LLM, no tokens).
    python -m scripts.eval_rules
Compares the Auditor's verdict with how often the rule actually fires WRONGLY on data/processed/tasks_v1.json."""
import argparse, json
from pathlib import Path

from src.domain.repo_repair.schema import RepoRepairTask
from src.domain.repo_repair.solver_tools import is_correct
from src.evaluation import evaluate_rule
from src.rules import Rule

ap = argparse.ArgumentParser()
ap.add_argument("--tasks", default="data/processed/tasks_v1.json")
ap.add_argument("--reports", default="runs/audit_reports.jsonl")
args = ap.parse_args()

tasks = [RepoRepairTask(**d) for d in json.loads(Path(args.tasks).read_text())]
latest = {}                                    # newest audit report per rule id wins
for line in Path(args.reports).read_text().splitlines():
    if line.strip():
        rep = json.loads(line)
        latest[rep["rule_id"]] = rep

print(f"{len(tasks)} tasks\n")
print(f"{'rule':14} {'action':12} {'audit verdict':14} {'fires':>6} {'false fires':>12} {'coverage':>9}")
for rid, rep in latest.items():
    rule = Rule.model_validate(rep["final_rule"])
    r = evaluate_rule(rule, tasks, is_correct)
    flag = "  <-- FALSE PASS" if rep["final_verdict"] == "passed" and r["false_fires"] else ""
    print(f"{rid:14} {rule.action['type']:12} {rep['final_verdict']:14} {r['fires']:6} "
          f"{r['false_fires']:12} {r['coverage']:9.0%}{flag}")
