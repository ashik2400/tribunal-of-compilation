"""Milestone 9: the three-way comparison on one task stream.

  no_shortcuts  every task goes to the Solver                      (cost baseline)
  naive         rules compiled after N consistent solves are installed STRAIGHT INTO PRODUCTION from the
                Compiler's unaudited drafts, with the registry's gates bypassed and first-match routing
  audited       the same rules, but the audited/repaired versions, installed through the real registry
                (fingerprint + provable-disjointness gates) and routed with its ambiguity guard

All three get the same shadow-check monitor. Everything is real code on real repos EXCEPT the LLM:
the Solver is replayed as correct (your measured accuracy) and cost is counted in estimated LLM calls
(measured mean steps per task). Re-compiling a demoted pattern is not simulated, which understates how
much of the naive condition's savings would recover after a detection.
"""
from __future__ import annotations

import itertools
import json
import random
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from src.auditor import AuditBackend, RegressionSuite, _hash
from src.compiler import dedupe_candidates, find_candidates
from src.registry import Entry, Registry, RegistryError, fingerprint
from src.router import Router
from src.rules import Rule
from src.shadow import ShadowChecker


def shape_key(action: dict) -> str:
    return json.dumps(action, sort_keys=True)


class NaiveRegistry(Registry):
    """What a system without the registry's safeguards does: apply the first matching rule, never ask whether
    other rules disagree."""

    def resolve(self, signature, captures, files):
        hits = self.match(signature, captures, files)
        return (hits[0], "first match") if hits else (None, "no production rule matches")


def build_stream(make_task: Callable[[], Any], n_first: int = 20, n_mixed_first: int = 20, n_mixed_twin: int = 20,
                 first_variant: str = "missing_package", twin_variant: str = "needs_local_import",
                 seed: int = 0, max_attempts: int = 5000) -> list:
    """Phase 1: n_first tasks of the first variant (so rules get compiled from a clean streak).
    Phase 2: a shuffled mix of the two variants (the twins arrive AFTER the rules exist)."""
    need = {first_variant: n_first + n_mixed_first, twin_variant: n_mixed_twin}
    got: dict[str, list] = {v: [] for v in need}
    for _ in range(max_attempts):
        if all(len(got[v]) >= need[v] for v in need):
            break
        t = make_task()
        if t.variant in got and len(got[t.variant]) < need[t.variant]:
            got[t.variant].append(t)
    if any(len(got[v]) < need[v] for v in need):
        raise RuntimeError(f"generator did not produce enough tasks: { {v: len(g) for v, g in got.items()} }")
    mixed = got[first_variant][n_first:] + got[twin_variant]
    random.Random(seed).shuffle(mixed)
    stream = got[first_variant][:n_first] + mixed
    # deterministic ids: shadow sampling hashes the task id, so a seed must reproduce the same experiment
    return [t.model_copy(update={"task_id": f"s{seed}_{i:03d}"}) for i, t in enumerate(stream)]


@dataclass
class ConditionResult:
    name: str
    timeline: list = field(default_factory=list)
    installs: list = field(default_factory=list)       # (task index, rule_id)
    blocked: list = field(default_factory=list)        # installs the registry refused
    demotions: list = field(default_factory=list)      # (task index, rule_id)
    shadow_events: list = field(default_factory=list)  # {i, task_id, rule_id, outcome}
    solver_cost: float = 0.0
    shadow_cost: float = 0.0
    metrics: dict = field(default_factory=dict)


def run_condition(name: str, tasks: list, library: dict[str, Rule], *, naive: bool,
                  is_correct: Callable[[Any, Any], bool], verify: Callable[[Any, dict], Any],
                  make_backend_fn: Callable[[Callable], AuditBackend], mean_steps: float,
                  truth_of: Callable[[Any], dict] = lambda t: t.ground_truth.action,
                  shadow_rate: float = 0.2, shadow_every: int = 5, k: int = 3, seed: int = 0,
                  demote_after: int = 1, n_required: int = 3) -> ConditionResult:
    """library maps a fix-shape (shape_key of a rule's action template) to the rule installed when the
    Compiler's streak for that shape completes. An empty library gives the no-shortcuts baseline."""
    truth_by_files = {_hash(t.files): truth_of(t) for t in tasks}
    backend = make_backend_fn(lambda task: (truth_by_files.get(_hash(task.files)), True))   # replayed oracle
    same = lambda a, b: backend.decision_key(a) == backend.decision_key(b)
    reg = (NaiveRegistry if naive else Registry)(None, same_decision=same)
    res = ConditionResult(name)
    tmp = tempfile.TemporaryDirectory()
    log = Path(tmp.name) / "router.jsonl"
    ticker = itertools.count(1)
    router = Router(reg, lambda t: (truth_of(t), True, 0), verify=verify, log_path=log,
                    clock=lambda: float(next(ticker)))
    checker = ShadowChecker(reg, backend, RegressionSuite(), k=k, sample_rate=shadow_rate,
                            demote_after=demote_after, seed=seed, log_path=Path(tmp.name) / "shadow.jsonl")
    runs, installed, seen_lines = [], set(), 0

    def install(rule: Rule, i: int) -> None:
        if naive:                                       # no audit, no gates: straight into production
            reg.entries[rule.rule_id] = Entry(rule=rule, state="production", fingerprint=fingerprint(rule),
                                              audit={"naive": True})
            res.installs.append((i, rule.rule_id))
            return
        try:
            reg.register(rule, {"final_verdict": "passed", "final_rule": rule.model_dump(), "rounds": []})
            reg.promote(rule.rule_id)
            res.installs.append((i, rule.rule_id))
        except RegistryError as e:
            res.blocked.append((i, rule.rule_id, str(e)[:120]))

    for i, task in enumerate(tasks):
        r = router.route(task)
        res.timeline.append({"i": i, "task_id": task.task_id, "variant": task.variant,
                             "handled_by": r.handled_by, "rule_id": r.rule_id,
                             "correct": bool(is_correct(task, r.fix))})
        if r.handled_by == "solver":
            res.solver_cost += mean_steps
            runs.append({"ts": i, "task_id": task.task_id, "traceback": task.traceback, "files": task.files,
                         "solution": r.fix, "verified": True, "confidence": 0.95})
            for cand in dedupe_candidates(find_candidates(runs, n_required)):
                key = shape_key(cand.shape)
                if key in library and key not in installed:
                    installed.add(key)                  # a demoted pattern is never re-installed (not simulated)
                    install(library[key], i)
        if (i + 1) % shadow_every == 0 or i == len(tasks) - 1:
            lines = log.read_text().splitlines()
            new = [json.loads(l) for l in lines[seen_lines:]]
            seen_lines = len(lines)
            for o in checker.run(new):
                if o.outcome == "skipped":
                    continue
                res.shadow_cost += k * mean_steps
                res.shadow_events.append({"i": i, "task_id": o.task_id, "rule_id": o.rule_id, "outcome": o.outcome})
                if o.demoted:
                    res.demotions.append((i, o.rule_id))
    tmp.cleanup()
    res.metrics = summarize(res, len(tasks))
    return res


def summarize(res: ConditionResult, n: int) -> dict:
    by_rule = [r for r in res.timeline if r["handled_by"] == "rule"]
    wrong_rule = [r for r in by_rule if not r["correct"]]
    first_mis = wrong_rule[0]["i"] if wrong_rule else None
    first_dem = res.demotions[0][0] if res.demotions else None
    checks = [e for e in res.shadow_events if e["outcome"] in ("agree", "diverge")]
    return {"tasks": n, "rule_handled": len(by_rule), "llm_free_pct": round(100 * len(by_rule) / n, 1),
            "wrong_rule_decisions": len(wrong_rule),
            "false_compilation_rate": round(len(wrong_rule) / len(by_rule), 3) if by_rule else 0.0,
            "wrong_total": sum(not r["correct"] for r in res.timeline),
            "first_misfire": first_mis, "first_demotion": first_dem,
            "detection_latency_tasks": (first_dem - first_mis) if first_mis is not None and first_dem is not None else None,
            "undetected": first_mis is not None and first_dem is None,
            "installs": len(res.installs), "blocked_installs": len(res.blocked),
            "shadow_checks": len(checks), "shadow_divergences": sum(e["outcome"] == "diverge" for e in checks),
            "solver_cost": round(res.solver_cost, 1), "shadow_cost": round(res.shadow_cost, 1),
            "llm_calls": round(res.solver_cost + res.shadow_cost, 1)}
