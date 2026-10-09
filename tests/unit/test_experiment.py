import itertools
import random
import pytest
from src.domain.repo_repair.audit_backend import make_backend
from src.domain.repo_repair.schema import GroundTruthFix, RepoRepairTask
from src.domain.repo_repair.solver_tools import is_correct, verify_fix_explained
from src.experiment import NaiveRegistry, build_stream, run_condition, shape_key
from tests.unit.test_auditor import IMP_C, LOCAL, MISSING, PIP_C, SIG, rule_a, rule_b
from tests.unit.test_registry import real_rule_a

TB = "ModuleNotFoundError: No module named 'colorutils'"
_ids = itertools.count()


def maker(seed=0):
    rng = random.Random(seed)
    def make():
        v = rng.choice(["missing_package", "needs_local_import"])
        files, truth = (MISSING, PIP_C) if v == "missing_package" else (LOCAL, IMP_C)
        return RepoRepairTask(task_id=f"t{next(_ids)}", bug_family="f", variant=v, files=files, entry_point="main.py",
                              traceback=TB, ground_truth=GroundTruthFix(description="d", action=truth))
    return make


def lib(*rules):
    return {shape_key(r.action): r for r in rules}


NAIVE_LIB = lib(rule_a(), rule_b())              # A as drafted: never checks for a local module
AUDITED_LIB = lib(real_rule_a(), rule_b())
KW = dict(is_correct=is_correct, verify=verify_fix_explained, make_backend_fn=lambda fn: make_backend(solve_fn=fn),
          mean_steps=5.0, shadow_rate=1.0, shadow_every=1, k=3)


def stream(n_first=6, n_mixed_first=4, n_twin=6, seed=0):
    return build_stream(maker(seed), n_first, n_mixed_first, n_twin, seed=seed)


# ---------- stream ----------
def test_stream_shape_twins_arrive_after_the_clean_phase():
    s = stream(6, 4, 6)
    assert len(s) == 16 and all(t.variant == "missing_package" for t in s[:6])
    tail = [t.variant for t in s[6:]]
    assert tail.count("needs_local_import") == 6 and tail.count("missing_package") == 4
    assert [t.task_id for t in stream(seed=3)] != [t.task_id for t in stream(seed=4)]
    assert [t.task_id for t in stream(seed=3)] == [t.task_id for t in stream(seed=3)]      # reproducible ids
    with pytest.raises(RuntimeError):
        build_stream(lambda: next(iter([])) if False else maker()(), 5, 5, 5, twin_variant="nope", max_attempts=50)


def test_naive_registry_applies_first_match_even_when_rules_disagree():
    r = NaiveRegistry(None)
    from src.registry import Entry, fingerprint
    for rule in (rule_a(), rule_b()):
        r.entries[rule.rule_id] = Entry(rule=rule, state="production", fingerprint=fingerprint(rule))
    assert r.resolve(SIG, ["colorutils"], LOCAL)[0].rule_id == "A"                # wrong one wins, silently


# ---------- the three conditions ----------
def test_no_shortcuts_sends_everything_to_the_solver():
    r = run_condition("no_shortcuts", stream(), {}, naive=False, **KW)
    assert r.metrics["rule_handled"] == 0 and r.metrics["llm_free_pct"] == 0.0 and r.metrics["wrong_total"] == 0
    assert r.metrics["llm_calls"] == 16 * 5.0 and r.shadow_events == []


def test_naive_compile_misfires_when_twins_arrive_then_shadow_check_demotes_it():
    r = run_condition("naive", stream(), NAIVE_LIB, naive=True, **KW)
    m = r.metrics
    assert r.installs[0][1] == "A" and r.installs[0][0] == 2                       # compiled after 3 consistent solves
    assert m["wrong_rule_decisions"] >= 1 and m["first_misfire"] >= 6               # only once a twin arrives
    assert m["first_demotion"] == m["first_misfire"] and m["detection_latency_tasks"] == 0   # every=1, rate=1
    assert m["undetected"] is False and m["wrong_rule_decisions"] == 1
    assert all(row["correct"] for row in r.timeline if row["i"] > m["first_demotion"])        # no more wrong fixes


def test_audited_compile_never_misfires_and_still_saves_calls():
    r = run_condition("audited", stream(), AUDITED_LIB, naive=False, **KW)
    m = r.metrics
    assert m["wrong_total"] == 0 and m["wrong_rule_decisions"] == 0 and m["demotions" if False else "first_demotion"] is None
    assert m["llm_free_pct"] > 0 and m["blocked_installs"] == 0 and [i[1] for i in r.installs] == ["A", "B"]
    assert m["shadow_divergences"] == 0 and m["shadow_checks"] > 0


def test_cost_accounting_includes_the_shadow_checks():
    r = run_condition("audited", stream(), AUDITED_LIB, naive=False, **KW)
    solver_tasks = sum(row["handled_by"] == "solver" for row in r.timeline)
    assert r.metrics["solver_cost"] == solver_tasks * 5.0
    assert r.metrics["shadow_cost"] == r.metrics["shadow_checks"] * 3 * 5.0
    assert r.metrics["llm_calls"] == r.metrics["solver_cost"] + r.metrics["shadow_cost"]


def test_low_sampling_can_miss_a_misfire_and_that_is_reported_honestly():
    r = run_condition("naive", stream(n_twin=6), NAIVE_LIB, naive=True, **{**KW, "shadow_rate": 0.0})
    assert r.metrics["undetected"] is True and r.metrics["wrong_rule_decisions"] >= 1
    assert r.metrics["false_compilation_rate"] > 0 and r.metrics["detection_latency_tasks"] is None


def test_runs_are_reproducible():
    a = run_condition("naive", stream(seed=2), NAIVE_LIB, naive=True, **{**KW, "shadow_rate": 0.4, "seed": 2})
    b = run_condition("naive", stream(seed=2), NAIVE_LIB, naive=True, **{**KW, "shadow_rate": 0.4, "seed": 2})
    key = lambda r: [(x["handled_by"], x["rule_id"], x["correct"]) for x in r.timeline]
    assert key(a) == key(b) and a.shadow_events == b.shadow_events
