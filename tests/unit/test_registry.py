import json
import pytest
from src.domain.repo_repair.audit_backend import decision_key
from src.registry import Registry, RegistryError, fingerprint
from src.rules import Predicate, Rule, provably_disjoint
from tests.unit.test_auditor import IMP, LOCAL, MISSING, PIP, SIG, rule_b


def real_rule_a():
    """Rule A exactly as your Compiler repaired it."""
    return Rule(rule_id="A", signature=SIG, action=PIP,
                scope=[Predicate(op="text_contains", glob="**/*.py", text="import {0}"),
                       Predicate(op="file_absent", glob="**/{0}.py"),
                       Predicate(op="file_absent", glob="**/{0}/__init__.py")])


def report(rule, verdict="passed", twins=("control:PASS",) * 3):
    return {"rule_id": rule.rule_id, "final_verdict": verdict, "final_rule": rule.model_dump(), "repairs_used": 1,
            "reason": "r", "rounds": [{"twins": [{"source": t.split(":")[0], "verdict": t.split(":")[1]} for t in twins]}]}


def reg(path=None):
    return Registry(path, same_decision=lambda a, b: decision_key(a) == decision_key(b), clock=lambda: 1.0)


def staged(*rules, path=None):
    r = reg(path)
    for rule in rules:
        r.register(rule, report(rule))
    return r


# ---------- disjointness proof ----------
def test_your_two_real_rules_are_provably_disjoint():
    assert provably_disjoint(real_rule_a(), rule_b())          # B needs utils/{0}.py; A forbids **/{0}.py


def test_overlap_that_cannot_be_ruled_out_is_not_called_disjoint():
    lazy = Rule(rule_id="L", signature=SIG, action=PIP, scope=[])
    assert not provably_disjoint(lazy, rule_b())
    assert not provably_disjoint(rule_b(), Rule(rule_id="B2", signature=SIG, action=IMP, scope=[
        Predicate(op="text_contains", glob="**/*.py", text="import {0}")]))
    assert provably_disjoint(lazy, Rule(rule_id="O", signature="other sig", action=PIP, scope=[]))


def test_text_predicates_can_prove_disjointness():
    a = Rule(rule_id="a", signature=SIG, action=PIP, scope=[Predicate(op="text_contains", glob="main.py", text="x")])
    b = Rule(rule_id="b", signature=SIG, action=IMP, scope=[Predicate(op="text_absent", glob="main.py", text="x")])
    assert provably_disjoint(a, b) and provably_disjoint(b, a)


# ---------- lifecycle ----------
def test_full_lifecycle_and_history(tmp_path):
    r = staged(real_rule_a(), rule_b(), path=tmp_path / "reg.json")
    assert {e.state for e in r.entries.values()} == {"staging"}
    r.promote("A"); r.promote("B")
    assert {x.rule_id for x in r.production_rules()} == {"A", "B"}
    r.demote("A", "shadow-check divergence")
    assert [x.rule_id for x in r.production_rules()] == ["B"]
    assert [e.to_state for e in r.entries["A"].history] == ["staging", "production", "demoted"]
    assert r.requeue_signatures() == [SIG]


def test_state_survives_reload(tmp_path):
    p = tmp_path / "reg.json"
    r = staged(real_rule_a(), path=p); r.promote("A")
    again = reg(p)
    assert again.entries["A"].state == "production" and again.entries["A"].audit["final_round_twins"] == {"control:PASS": 3}
    assert json.loads(p.read_text())["schema_version"] == 1


# ---------- fail-closed gates ----------
def test_only_passed_audits_can_register():
    for verdict in ("deferred", "broken", "inconclusive"):
        with pytest.raises(RegistryError, match="not 'passed'"):
            reg().register(real_rule_a(), report(real_rule_a(), verdict))


def test_rule_must_match_the_audited_version():
    audited = real_rule_a()
    edited = audited.model_copy(update={"scope": audited.scope[:1]})      # someone loosened the scope
    with pytest.raises(RegistryError, match="differs from the version that was audited"):
        reg().register(edited, report(audited))


def test_rule_edited_after_staging_cannot_be_promoted():
    r = staged(real_rule_a())
    r.entries["A"].rule.scope.pop()                                       # tampering after registration
    with pytest.raises(RegistryError, match="modified after its audit"):
        r.promote("A")


def test_conflicting_rule_is_blocked_from_production():
    lazy = Rule(rule_id="L", signature=SIG, action=PIP, scope=[Predicate(op="file_exists", glob="main.py")])
    r = staged(rule_b(), lazy)
    r.promote("B")
    with pytest.raises(RegistryError, match="not provably disjoint"):
        r.promote("L")


def test_overlap_is_allowed_when_both_rules_decide_the_same_thing():
    twin_of_b = rule_b().model_copy(update={"rule_id": "B2", "scope": rule_b().scope[:1]})
    r = staged(rule_b(), twin_of_b)
    r.promote("B"); r.promote("B2")                                       # redundant, never contradictory
    assert {x.rule_id for x in r.production_rules()} == {"B", "B2"}


def test_invalid_transitions_are_refused():
    r = staged(real_rule_a())
    with pytest.raises(RegistryError):
        r.demote("A", "  ")                                               # reason required
    r.promote("A")
    with pytest.raises(RegistryError, match="only staging"):
        r.promote("A")
    r.demote("A", "x")
    with pytest.raises(RegistryError, match="already demoted"):
        r.demote("A", "y")
    with pytest.raises(RegistryError, match="unknown rule"):
        r.promote("nope")


def test_registering_twice_is_idempotent_but_a_different_version_needs_a_demotion():
    a = real_rule_a()
    r = staged(a)
    assert r.register(a, report(a)).version == 1
    narrower = a.model_copy(update={"scope": a.scope + [Predicate(op="file_absent", glob="requirements.txt")]})
    with pytest.raises(RegistryError, match="demote it first"):
        r.register(narrower, report(narrower))
    r.promote("A"); r.demote("A", "drift")
    e = r.register(narrower, report(narrower))
    assert e.version == 2 and e.state == "staging" and fingerprint(e.rule) == e.fingerprint


# ---------- runtime view ----------
def test_resolve_picks_the_right_rule_and_refuses_when_unsure():
    r = staged(real_rule_a(), rule_b()); r.promote("A"); r.promote("B")
    rule, why = r.resolve(SIG, ["colorutils"], MISSING)
    assert rule.rule_id == "A" and why == "ok"
    assert r.resolve(SIG, ["colorutils"], LOCAL)[0].rule_id == "B"
    assert r.resolve(SIG, ["colorutils"], {"main.py": "print(1)\n"})[0] is None          # nothing matches
    # simulate a (blocked-in-practice) overlap to prove the runtime guard also fails closed
    lazy = Rule(rule_id="L", signature=SIG, action=PIP, scope=[])
    r.entries["L"] = r.entries["A"].model_copy(update={"rule": lazy})
    rule, why = r.resolve(SIG, ["colorutils"], LOCAL)
    assert rule is None and "ambiguous" in why


def test_demoted_and_staging_rules_never_fire():
    r = staged(real_rule_a())
    assert r.resolve(SIG, ["colorutils"], MISSING)[0] is None            # staging only
    r.promote("A"); r.demote("A", "x")
    assert r.resolve(SIG, ["colorutils"], MISSING)[0] is None
