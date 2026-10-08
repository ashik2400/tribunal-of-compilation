"""Manage the rule registry.
    python -m scripts.registry_cli sync                  register every audit-passed rule as STAGING
    python -m scripts.registry_cli promote --all         staging -> production (conflict-checked)
    python -m scripts.registry_cli promote rule_xxxx
    python -m scripts.registry_cli demote rule_xxxx --reason "why"
    python -m scripts.registry_cli list
"""
import argparse, json, sys
from pathlib import Path

from src.domain.repo_repair.audit_backend import decision_key
from src.registry import Registry, RegistryError
from src.rules import Rule

ap = argparse.ArgumentParser()
ap.add_argument("cmd", choices=["sync", "promote", "demote", "list"])
ap.add_argument("rule_id", nargs="?")
ap.add_argument("--all", action="store_true")
ap.add_argument("--reason", default="")
ap.add_argument("--registry", default="rules/registry.json")
ap.add_argument("--reports", default="runs/audit_reports.jsonl")
args = ap.parse_args()

reg = Registry(args.registry, same_decision=lambda a, b: decision_key(a) == decision_key(b))


def show() -> None:
    print(f"{'rule':16} {'state':11} {'ver':>3} {'action':12} scope")
    for rid, e in reg.entries.items():
        scope = "; ".join(f"{p.op} {p.glob}" + (f" '{p.text}'" if p.text else "") for p in e.rule.scope) or "EMPTY"
        print(f"{rid:16} {e.state:11} {e.version:3} {e.rule.action['type']:12} {scope}")


try:
    if args.cmd == "sync":
        latest = {}
        for line in Path(args.reports).read_text().splitlines():
            if line.strip():
                rep = json.loads(line)
                latest[rep["rule_id"]] = rep                      # newest report per rule wins
        for rid, rep in latest.items():
            if rep["final_verdict"] != "passed":
                print(f"skip {rid}: audit verdict {rep['final_verdict']}")
                continue
            e = reg.register(Rule.model_validate(rep["final_rule"]), rep)
            print(f"{rid}: {e.state} (v{e.version})")
    elif args.cmd == "promote":
        ids = [i for i, e in reg.entries.items() if e.state == "staging"] if args.all else [args.rule_id]
        for rid in ids:
            try:
                reg.promote(rid)
                print(f"{rid}: promoted to production")
            except RegistryError as e:
                print(f"{rid}: BLOCKED - {e}")
    elif args.cmd == "demote":
        reg.demote(args.rule_id, args.reason)
        print(f"{args.rule_id}: demoted")
except RegistryError as e:
    sys.exit(f"error: {e}")
show()
