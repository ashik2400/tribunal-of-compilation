import json
from src.auditor import RegressionSuite
from src.ci_checks import check_registry
from src.domain.repo_repair.audit_backend import decision_key, make_backend
from src.domain.repo_repair.solver_tools import verify_fix_explained
from src.registry import Registry
from src.router import Router
from src.rules import Predicate, Rule
from src.shadow import ShadowChecker, divergence_curve, sampled
from tests.unit.test_auditor import IMP_C, LOCAL, PIP, PIP_C, SIG, oracle
from tests.unit.test_registry import report
from tests.unit.test_router import T_LOCAL, T_MISSING, registry


def lazy_registry():
    """What a naive compiler would ship: pip_install for anything that imports the missing module."""
    lazy = Rule(rule_id="L", signature=SIG, action=PIP,
                scope=[Predicate(op="text_contains", glob="**/*.py", text="import {0}")])
    r = Registry(None, same_decision=lambda a, b: decision_key(a) == decision_key(b))
    r.register(lazy, report(lazy)); r.promote("L")
    return r


def stream(reg, tasks, tmp_path):
    log = tmp_path / "router.jsonl"
    router = Router(reg, lambda t: (IMP_C, True, 5), verify=verify_fix_explained, log_path=log)
    for t in tasks:
        router.route(t)
    return [json.loads(l) for l in log.read_text().splitlines()]


def checker(reg, tmp_path, solve=oracle, regression=None, **kw):
    kw = {"k": 3, "sample_rate": 1.0, **kw}
    return ShadowChecker(reg, make_backend(solve_fn=solve), regression, log_path=tmp_path / "shadow.jsonl", **kw)


def local(i):
    return T_LOCAL.model_copy(update={"task_id": f"local{i}"})


def test_router_log_now_carries_the_entry_point(tmp_path):
    assert stream(registry(), [T_MISSING], tmp_path)[0]["entry_point"] == "main.py"


def test_healthy_rules_agree_and_stay_in_production(tmp_path):
    reg = registry()
    out = checker(reg, tmp_path).run(stream(reg, [T_MISSING, T_LOCAL], tmp_path))
    assert [o.outcome for o in out] == ["agree", "agree"] and not any(o.demoted for o in out)
    assert reg.entries["A"].stats["shadow_checks"] == 1 and reg.entries["A"].stats["divergences"] == 0
    assert {e.state for e in reg.entries.values()} == {"production"}


def test_a_silently_wrong_rule_is_caught_demoted_and_remembered(tmp_path):
    reg, suite = lazy_registry(), RegressionSuite()
    records = stream(reg, [T_LOCAL], tmp_path)
    assert records[0]["handled_by"] == "rule" and records[0]["fix"]["type"] == "pip_install"   # the silent error
    out = checker(reg, tmp_path, regression=suite).run(records)
    assert out[0].outcome == "diverge" and out[0].demoted and out[0].solver_fix["type"] == "fix_import"
    assert reg.entries["L"].state == "demoted" and "shadow-check" in reg.entries["L"].history[-1].reason
    assert suite.records[0]["files"] == LOCAL and suite.records[0]["correct_fix"]["type"] == "fix_import"
    # loop closed: the next identical failure goes to the Solver, and the CI gate would refuse a re-promotion
    assert Router(reg, lambda t: (IMP_C, True, 5)).route(T_LOCAL).handled_by == "solver"
    reg.entries["L"].state = "production"
    assert any("misfires on stored twin" in p for p in check_registry(reg, suite, make_backend()))


def test_inconclusive_checks_are_never_held_against_a_rule(tmp_path):
    reg = lazy_registry()
    flip = iter([0, 1] * 20)
    def flaky(task):
        return (oracle(task) if next(flip) == 0 else ({"type": "pip_install", "package": "colorutils"}, True))
    out = checker(reg, tmp_path, solve=flaky).run(stream(reg, [T_LOCAL], tmp_path))
    assert out[0].outcome == "inconclusive" and not out[0].demoted
    assert reg.entries["L"].state == "production" and reg.entries["L"].stats["divergences"] == 0
    assert reg.entries["L"].stats["inconclusive"] == 1


def test_demote_after_requires_that_many_confirmed_divergences(tmp_path):
    reg = lazy_registry()
    records = stream(reg, [local(1), local(2)], tmp_path)
    c = checker(reg, tmp_path, demote_after=2)
    first = c.run(records[:1])
    assert first[0].outcome == "diverge" and not first[0].demoted and reg.entries["L"].state == "production"
    second = c.run(records)
    assert second[0].demoted and reg.entries["L"].state == "demoted"       # record 1 already logged: not re-checked


def test_sampling_is_deterministic_and_capped(tmp_path):
    assert sampled(1, "t", 5.0, 0.5) == sampled(1, "t", 5.0, 0.5)
    hits = sum(sampled(0, f"t{i}", 1.0, 0.5) for i in range(400))
    assert 140 < hits < 260 and sampled(0, "x", 1.0, 1.0) and not sampled(0, "x", 1.0, 0.0)
    reg = registry()
    recs = stream(reg, [T_MISSING.model_copy(update={"task_id": f"m{i}"}) for i in range(5)], tmp_path)
    assert checker(registry(), tmp_path, sample_rate=0.0).run(recs) == []
    assert len(checker(registry(), tmp_path / "x", max_checks=2).run(recs)) == 2


def test_rechecking_is_idempotent(tmp_path):
    reg = registry()
    recs = stream(reg, [T_MISSING], tmp_path)
    c = checker(reg, tmp_path)
    assert len(c.run(recs)) == 1 and c.run(recs) == [] and reg.entries["A"].stats["shadow_checks"] == 1


def test_only_live_rule_decisions_are_checked(tmp_path):
    reg = registry()
    recs = stream(reg, [T_MISSING, local(1)], tmp_path) + [{"ts": 1.0, "task_id": "s", "handled_by": "solver", "rule_id": None}]
    reg.demote("B", "x")                                                     # B no longer protects anything
    out = checker(reg, tmp_path).run(recs)
    assert [o.rule_id for o in out] == ["A"]


def test_unreplayable_repos_are_skipped_not_counted(tmp_path):
    reg = registry()
    rec = stream(reg, [T_MISSING], tmp_path)[0]
    rec["files"] = {"main.py": "print('fine')\n"}                           # no longer fails
    out = checker(reg, tmp_path).run([rec])
    assert out[0].outcome == "skipped" and reg.entries["A"].stats["shadow_checks"] == 0


def test_divergence_curve_is_cumulative_and_ignores_uninformative_checks(tmp_path):
    reg = lazy_registry()
    c = checker(reg, tmp_path, demote_after=99)
    c.run(stream(reg, [T_MISSING, local(1), T_MISSING.model_copy(update={"task_id": "m2"}), local(2)], tmp_path))
    curve = divergence_curve(tmp_path / "shadow.jsonl")
    assert [(r["checks"], r["divergences"]) for r in curve] == [(1, 0), (2, 1), (3, 1), (4, 2)]
    assert curve[-1]["rate"] == 0.5
