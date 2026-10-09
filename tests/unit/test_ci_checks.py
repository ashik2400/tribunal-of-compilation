import json
import subprocess
import sys
from src.auditor import RegressionSuite
from src.ci_checks import check_registry
from src.domain.repo_repair.audit_backend import decision_key, make_backend
from src.registry import Registry, fingerprint
from src.rules import Predicate, Rule
from tests.unit.test_auditor import IMP_C, LOCAL, PIP, SIG, rule_b
from tests.unit.test_registry import real_rule_a, report
from tests.unit.test_router import registry

TWIN = {"signature": SIG, "files": LOCAL, "entry_point": "main.py", "correct_fix": IMP_C, "why": "local module"}


def suite(*records):
    s = RegressionSuite()
    for r in records:
        s.add(r)
    return s


def test_your_real_registry_state_passes_the_gate():
    assert check_registry(registry(), suite(TWIN), make_backend()) == []


def test_a_bad_rule_in_production_fails_the_regression_gate():
    lazy = Rule(rule_id="L", signature=SIG, action=PIP, scope=[Predicate(op="text_contains", glob="**/*.py", text="import {0}")])
    r = Registry(None, same_decision=lambda a, b: decision_key(a) == decision_key(b))
    r.register(lazy, report(lazy)); r.promote("L")
    problems = check_registry(r, suite(TWIN), make_backend())
    assert any("misfires on stored twin" in p for p in problems)


def test_tampering_is_caught():
    r = registry()
    r.entries["A"].rule.scope.pop()
    assert any("fingerprint mismatch" in p for p in check_registry(r, suite(), make_backend()))


def test_forced_conflicting_production_rules_are_caught():
    r = registry()

    def force(rid, **changes):                      # bypass the registry's own gates, as a bad merge could
        e = r.entries[rid]
        e.rule = e.rule.model_copy(update=changes)
        e.fingerprint = fingerprint(e.rule)

    force("A", action={"type": "fix_import", "replace_line": "import {0}", "with_line": "from utils import {0}"})
    force("B", action=PIP, scope=[])                # opposite decisions, scopes no longer provably disjoint
    assert any("may overlap and decide differently" in p for p in check_registry(r, suite(), make_backend()))


def test_cli_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    registry(good)
    twins = tmp_path / "twins.jsonl"
    twins.write_text(json.dumps(TWIN) + "\n")
    run = lambda *a: subprocess.run([sys.executable, "-m", "scripts.check_registry", *a], capture_output=True, text=True)
    ok = run("--registry", str(good), "--regression", str(twins))
    assert ok.returncode == 0 and "registry OK" in ok.stdout
    assert run("--registry", str(tmp_path / "missing.json")).returncode == 0       # nothing to check yet
    data = json.loads(good.read_text())
    data["entries"]["A"]["rule"]["scope"] = []                                      # loosened after audit
    good.write_text(json.dumps(data))
    bad = run("--registry", str(good), "--regression", str(twins))
    assert bad.returncode == 1 and "FAIL" in bad.stdout
