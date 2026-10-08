import json
import pytest
from src.domain.repo_repair.audit_backend import decision_key
from src.domain.repo_repair.schema import GroundTruthFix, RepoRepairTask
from src.domain.repo_repair.solver_tools import is_correct, verify_fix_explained
from src.registry import Registry
from src.router import Router
from tests.unit.test_auditor import IMP, IMP_C, LOCAL, MISSING, PIP, PIP_C, rule_b
from tests.unit.test_registry import real_rule_a, report

TB = "ModuleNotFoundError: No module named 'colorutils'"


def task(task_id, files, truth, tb=TB):
    return RepoRepairTask(task_id=task_id, bug_family="f", variant=task_id, files=files, entry_point="main.py",
                          traceback=tb, ground_truth=GroundTruthFix(description="d", action=truth))


T_MISSING = task("missing", MISSING, PIP_C)
T_LOCAL = task("local", LOCAL, IMP_C)
T_VENDOR = task("vendor", {**MISSING, "vendor/colorutils/__init__.py": "x = 1\n"}, IMP_C)    # no rule covers this


def registry(path=None, promote=("A", "B")):
    r = Registry(path, same_decision=lambda a, b: decision_key(a) == decision_key(b))
    for rule in (real_rule_a(), rule_b()):
        r.register(rule, report(rule))
    for rid in promote:
        r.promote(rid)
    return r


def no_solver(task):
    raise AssertionError("the Solver must not be called")


def test_covered_tasks_cost_zero_llm_calls_and_are_correct():
    router = Router(registry(), no_solver, verify=verify_fix_explained)
    for t in (T_MISSING, T_LOCAL):
        res = router.route(t)
        assert res.handled_by == "rule" and res.llm_calls == 0 and res.verified is True
        assert is_correct(t, res.fix)
    assert router.route(T_MISSING).rule_id == "A" and router.route(T_LOCAL).rule_id == "B"


def test_uncovered_task_falls_back_to_the_solver():
    calls = []
    def solver(t):
        calls.append(t.task_id)
        return IMP_C, True, 5
    res = Router(registry(), solver, verify=verify_fix_explained).route(T_VENDOR)
    assert res.handled_by == "solver" and res.llm_calls == 5 and calls == ["vendor"]
    assert "no production rule matches" in res.reason


def test_a_rule_whose_fix_crashes_is_not_trusted_and_is_reported():
    broken = real_rule_a().model_copy(update={"action": {"type": "pip_install", "package": "not-on-the-index"}})
    r = Registry(None, same_decision=lambda a, b: decision_key(a) == decision_key(b))
    r.entries["A"] = registry().entries["A"].model_copy(update={"rule": broken})
    r.entries["A"].state = "production"
    res = Router(r, lambda t: (PIP_C, True, 4), verify=verify_fix_explained).route(T_MISSING)
    assert res.handled_by == "solver" and res.failed_rule_id == "A" and "did not run" in res.reason


def test_staging_and_demoted_rules_are_never_used():
    staged = registry(promote=())
    res = Router(staged, lambda t: (PIP_C, True, 3)).route(T_MISSING)
    assert res.handled_by == "solver"
    live = registry(); live.demote("A", "drift")
    assert Router(live, lambda t: (PIP_C, True, 3)).route(T_MISSING).handled_by == "solver"
    assert Router(live, no_solver).route(T_LOCAL).handled_by == "rule"            # B unaffected


def test_demotion_takes_effect_on_the_next_decision(tmp_path):
    p = tmp_path / "reg.json"
    registry(p)
    solver_calls = []
    def solver(t):
        solver_calls.append(1)
        return PIP_C, True, 3
    load = lambda: Registry(p, same_decision=lambda a, b: decision_key(a) == decision_key(b))
    assert Router(load(), solver).route(T_MISSING).handled_by == "rule"
    reg = load(); reg.demote("A", "shadow-check divergence")
    assert Router(load(), solver).route(T_MISSING).handled_by == "solver" and solver_calls == [1]


def test_every_decision_is_logged_with_the_repo_for_shadow_checks(tmp_path):
    log = tmp_path / "router.jsonl"
    router = Router(registry(), lambda t: (IMP_C, True, 5), verify=verify_fix_explained, log_path=log, clock=lambda: 9.0)
    router.route(T_MISSING); router.route(T_VENDOR)
    recs = [json.loads(l) for l in log.read_text().splitlines()]
    assert [r["handled_by"] for r in recs] == ["rule", "solver"]
    assert recs[0]["rule_id"] == "A" and recs[0]["files"] == MISSING and recs[0]["traceback"] == TB and recs[0]["ts"] == 9.0
