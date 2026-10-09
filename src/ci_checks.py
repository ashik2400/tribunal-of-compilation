"""Safety gates run by CI on every push. Pure checks: no LLM, no network, no API key.

A registry passes only if:
  1. integrity   - every rule still matches the fingerprint it was audited and registered with;
  2. no conflict - any two production rules on one signature are provably disjoint or decide the same thing;
  3. regression  - no production rule misfires on any stored twin that ever broke a rule.
"""
from __future__ import annotations

from src.auditor import AuditBackend, RegressionSuite
from src.registry import Registry, fingerprint
from src.rules import provably_disjoint


def check_registry(registry: Registry, regression: RegressionSuite, backend: AuditBackend) -> list[str]:
    problems: list[str] = []
    for rid, e in registry.entries.items():
        if fingerprint(e.rule) != e.fingerprint:
            problems.append(f"{rid}: rule was modified after its audit (fingerprint mismatch)")

    prod = [e.rule for e in registry.entries.values() if e.state == "production"]
    for i, a in enumerate(prod):
        for b in prod[i + 1:]:
            if not provably_disjoint(a, b) and not registry.same_decision(a.action, b.action):
                problems.append(f"{a.rule_id} and {b.rule_id}: production rules may overlap and decide differently")

    for rule in prod:
        for rec in regression.failures(rule, backend):
            problems.append(f"{rule.rule_id}: misfires on stored twin {sorted(rec['files'])} "
                            f"(correct fix: {rec['correct_fix']})")
    return problems
