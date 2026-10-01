"""Auditor: tries to break a drafted rule before it is trusted. Fails closed.

Pipeline for one rule
  1. vacuity check     - the rule must still fire on the examples it was compiled from
  2. regression replay - every twin that ever broke a rule is replayed first (no LLM, cheap)
  3. twin generation   - one LLM call proposes repos that satisfy the scope but may need a different fix
  4. gates (mechanical, no LLM) - a twin counts only if it
       * builds and really fails, with the SAME error signature as the rule
       * lies inside the rule's scope, is new, and its claimed fix really works when executed
  5. verdict per twin
       BREAK        the rule's own fix fails to run there, OR the Solver's unanimous verified answer and the
                    Auditor's claimed fix agree with each other and disagree with the rule (two independent votes)
       PASS         the rule's fix runs AND the Solver's unanimous verified answer agrees with the rule
       INCONCLUSIVE Solver runs disagree / unverified / Solver and Auditor disagree ("disputed")
       INVALID      failed a gate (never counted)
  6. verdict per rule: any BREAK -> broken; >= min_pass PASS and no BREAK -> passed; otherwise inconclusive
     (inconclusive is NOT promoted: uncertainty defers).
  7. broken rules get up to `max_repairs` Compiler repairs; each repair must still cover the original
     examples and is re-audited from scratch (including all stored breaking twins).

The Solver is the oracle for "what is the right fix", as agreed, but never the only vote.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Hashable

from src.compiler import Candidate, Compiler
from src.llm import LLM
from src.rules import Rule, signature_of
from src.solver import _parse_json


@dataclass
class AuditBackend:
    """Everything domain-specific the Auditor needs. repo_repair provides one (audit_backend.py)."""
    build_task: Callable[[dict, str, str], Any]           # (files, entry_point, task_id) -> task; raises if it doesn't fail
    verify: Callable[[Any, dict], tuple[bool, str]]       # execute a fix on the task
    solve: Callable[[Any], tuple[dict | None, bool]]      # one Solver run -> (fix, verified)
    decision_key: Callable[[dict], Hashable]              # what "same decision" means
    validate_fix: Callable[[Any], str | None]
    notes: str = ""                                       # domain vocabulary shown to the twin generator
    # optional: deterministic twin operators. (files, quoted names) -> [(mutated files, why)]. No LLM involved.
    mutate: Callable[[dict, list], list] | None = None
    # optional: benign perturbations that must NOT change the right answer ("control" twins). Same signature.
    controls: Callable[[dict, list], list] | None = None


@dataclass
class TwinResult:
    files: dict
    verdict: str                     # PASS | BREAK | INCONCLUSIVE | INVALID
    reason: str
    entry_point: str = ""
    expected_fix: dict | None = None
    rule_fix: dict | None = None
    solver_fix: dict | None = None
    solver_runs: list = field(default_factory=list)   # [{fix, verified}] per oracle run, for debugging
    why: str = ""
    source: str = "llm"              # llm | mutation | control


@dataclass
class Round:
    rule: dict
    verdict: str = ""                # passed | broken | inconclusive | vacuous
    reason: str = ""
    twins: list[TwinResult] = field(default_factory=list)
    breaks: list[dict] = field(default_factory=list)      # {files, entry_point, rule_fix, correct_fix, why}
    regression_failures: int = 0
    gen_replies: list[str] = field(default_factory=list)   # raw twin-generator output, for debugging


@dataclass
class AuditReport:
    rule_id: str
    signature: str
    final_verdict: str               # passed (eligible for the registry) | deferred (stays agentic)
    reason: str
    initial_rule: dict
    final_rule: dict
    repairs_used: int
    rounds: list[Round] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _unescape(text: str) -> str:
    """LLMs sometimes double-escape JSON, leaving a literal backslash-n where a newline belongs."""
    if "\n" not in text and ("\\n" in text or "\\t" in text):
        return text.replace("\\n", "\n").replace("\\t", "\t")
    return text


def _hash(files: dict) -> str:
    return hashlib.sha1(json.dumps(files, sort_keys=True).encode()).hexdigest()


class RegressionSuite:
    """Every twin that ever broke a rule. Replayed on every audit and (later) in CI."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path else None
        self.records: list[dict] = []
        if self.path and self.path.exists():
            self.records = [json.loads(l) for l in self.path.read_text().splitlines() if l.strip()]

    def add(self, rec: dict) -> None:
        if any(_hash(r["files"]) == _hash(rec["files"]) for r in self.records):
            return
        self.records.append(rec)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(rec) + "\n")

    def failures(self, rule: Rule, backend: AuditBackend) -> list[dict]:
        out = []
        for rec in self.records:
            if rec["signature"] != rule.signature:
                continue
            try:
                task = backend.build_task(rec["files"], rec["entry_point"], "regress_" + _hash(rec["files"])[:8])
            except Exception:
                continue                                     # environment changed; can't replay this one
            sig, caps = signature_of(task.traceback)
            if not rule.matches(sig, caps, rec["files"]):
                continue                                     # out of scope now: the repair excluded it
            fix = rule.fix(caps)
            ok, _ = backend.verify(task, fix)
            if not ok or (rec["correct_fix"] is not None
                          and backend.decision_key(fix) != backend.decision_key(rec["correct_fix"])):
                out.append(rec)
        return out


SYSTEM = """You are the Auditor. Your job is to BREAK a shortcut rule before it is trusted.
A rule fires when a failure has the same signature AND every scope condition holds; then it applies a fixed
fix template automatically, with no oversight. Find repos where ALL scope conditions hold and the error message
is identical, yet the correct fix is DIFFERENT from the rule's fix. Think about what the scope does NOT check.
Reply with ONE JSON object and nothing else:
  {"twins": [{"files": {"path": "content", ...}, "entry_point": "main.py",
              "expected_fix": <a fix object>, "why": "<one sentence>"}, ...]}
Repos must be tiny (at most 4 files, each under 600 characters), must FAIL when entry_point runs with the same
error, and must not use os, sys, subprocess, the network or file access. Make the twins different from each other
and from the examples given."""


class Auditor:
    def __init__(self, llm: LLM, backend: AuditBackend, k: int = 3, n_twins: int = 5, min_pass: int = 3,
                 gen_rounds: int = 4, per_round: int = 6, max_mutations: int = 3,
                 max_controls: int = 3):
        self.llm, self.backend = llm, backend
        self.max_mutations, self.max_controls = max_mutations, max_controls
        self.k, self.n_twins, self.min_pass = k, n_twins, min_pass
        self.gen_rounds, self.per_round = gen_rounds, per_round

    # ---------- twin generation ----------
    def _gen_prompt(self, rule: Rule, cand: Candidate, feedback: list[str]) -> str:
        parts = [f"Failure signature: {rule.signature}",
                 f"Rule scope: {json.dumps([p.model_dump(exclude_none=True) for p in rule.scope])}",
                 f"Rule fix template: {json.dumps(rule.action)}   ({{0}}, {{1}}.. = quoted names in the error)",
                 f"Rule rationale: {rule.rationale}", "", "Repos the rule was compiled from:"]
        for ex in cand.examples[:3]:
            parts.append(f"  quoted names = {ex['captures']}; files: {json.dumps(ex['files'])}")
        if self.backend.notes:
            parts += ["", self.backend.notes]
        if feedback:
            parts += ["", "Earlier proposals were rejected (learn from each; do not repeat them):"] + feedback[-8:]
        parts.append(f"\nPropose {self.per_round} twins. Each must differ STRUCTURALLY from the example repos and "
                     "from each other (add, remove or move files), not just in wording.")
        return "\n".join(parts)

    # ---------- one twin ----------
    def _consensus(self, task, k: int):
        """Run the Solver k times. Returns (fix or None, runs, why-not)."""
        solves = [self.backend.solve(task) for _ in range(k)]
        runs = [{"fix": fix, "verified": ok} for fix, ok in solves]
        if not all(fix and ok for fix, ok in solves):
            return None, runs, "Solver did not produce a verified fix on every run"
        if len({self.backend.decision_key(fix) for fix, _ in solves}) != 1:
            return None, runs, "Solver runs disagreed with each other"
        return solves[0][0], runs, ""

    def _evaluate(self, rule: Rule, raw: Any, seen: set, example_hashes: set, k: int | None = None) -> TwinResult:
        b = self.backend
        raw = raw if isinstance(raw, dict) else {}
        files, entry, expected = raw.get("files"), raw.get("entry_point"), raw.get("expected_fix")
        why, source = str(raw.get("why", "")), raw.get("source", "llm")
        has_claim = source == "llm"       # mutation twins carry no Auditor claim: the Solver alone judges them

        def bad(reason: str) -> TwinResult:
            return TwinResult(files if isinstance(files, dict) else {}, "INVALID", reason,
                              entry_point=str(entry or ""), why=why, source=source)

        if not isinstance(files, dict) or not files or not all(
                isinstance(k_, str) and isinstance(v, str) for k_, v in files.items()):
            return bad("files must be a non-empty {path: text} object")
        files = {k_: _unescape(v) for k_, v in files.items()}
        if not isinstance(entry, str) or entry not in files:
            return bad("entry_point must be one of the files")
        if has_claim:
            err = b.validate_fix(expected)
            if err:
                return bad(f"expected_fix invalid: {err}")
        h = _hash(files)
        if h in example_hashes:
            return bad("identical to a given example repo; propose a structurally different one")
        if h in seen:
            return bad("duplicate of an earlier proposal")
        seen.add(h)
        try:
            task = b.build_task(files, entry, f"twin_{h[:8]}")
        except Exception as e:
            return bad(f"cannot build/run repo: {str(e)[:150]}")
        sig, caps = signature_of(task.traceback)
        if sig != rule.signature:
            return bad(f"error signature differs: {sig}")
        if not rule.matches(sig, caps, files):
            return bad("repo is outside the rule's scope")
        if has_claim:
            exp_ok, exp_why = b.verify(task, expected)
            if not exp_ok:
                return bad(f"expected_fix does not actually fix the repo: {exp_why}")

        rule_fix = rule.fix(caps)
        res = TwinResult(files, "", "", entry_point=entry, expected_fix=expected if has_claim else None,
                         rule_fix=rule_fix, why=why, source=source)
        k = k or self.k
        rule_ok, rule_why = b.verify(task, rule_fix)
        if not rule_ok:
            res.verdict, res.reason = "BREAK", f"the rule's own fix fails to run here: {rule_why}"
            if not has_claim:             # best effort: learn what the right fix was, for repair + regression
                res.expected_fix, res.solver_runs, _ = self._consensus(task, k)
            return res

        fix, res.solver_runs, why_not = self._consensus(task, k)
        if fix is None:
            res.verdict, res.reason = "INCONCLUSIVE", why_not
            return res
        res.solver_fix = fix
        skey = b.decision_key(fix)
        if skey == b.decision_key(rule_fix):
            res.verdict, res.reason = "PASS", "rule's fix runs and the Solver unanimously agrees"
        elif not has_claim:
            res.verdict = "BREAK"
            res.reason = f"Solver unanimously ({k} runs) chose a different fix than the rule (mutation twin)"
            res.expected_fix = fix
        elif skey == b.decision_key(expected):
            res.verdict, res.reason = "BREAK", "Solver (unanimous) and Auditor both say a different fix is correct"
        else:
            res.verdict, res.reason = "INCONCLUSIVE", "disputed: Solver and Auditor disagree about the right fix"
        return res

    # ---------- one audit round ----------
    def audit_round(self, rule: Rule, cand: Candidate, regression: RegressionSuite | None = None) -> Round:
        rd = Round(rule=rule.model_dump())
        for ex in cand.examples:                                   # 1. vacuity
            if not rule.matches(cand.signature, ex["captures"], ex["files"]):
                rd.verdict, rd.reason = "vacuous", f"rule no longer matches its own example {ex['task_id']}"
                return rd
        if regression:                                             # 2. regression replay
            fails = regression.failures(rule, self.backend)
            if fails:
                rd.regression_failures = len(fails)
                rd.breaks = [{"files": r["files"], "entry_point": r["entry_point"], "rule_fix": None,
                              "correct_fix": r["correct_fix"], "why": r.get("why", "known breaking twin")} for r in fails]
                rd.verdict, rd.reason = "broken", f"fails {len(fails)} stored regression twin(s)"
                return rd

        example_hashes = {_hash(ex["files"]) for ex in cand.examples}   # 3. twins: mutation first, then LLM
        seen: set = set()
        feedback: list[str] = []
        valid = 0

        def record_break(t: TwinResult) -> None:
            rd.breaks.append({"files": t.files, "entry_point": t.entry_point, "rule_fix": t.rule_fix,
                              "correct_fix": t.expected_fix, "why": t.reason})
            if regression:
                regression.add({"signature": rule.signature, "files": t.files, "entry_point": t.entry_point,
                                "correct_fix": t.expected_fix, "why": t.reason, "origin_rule": rule.rule_id})

        def run_batch(items: list, source: str, limit: int) -> None:
            """Judge deterministic twins (no Auditor claim: the Solver alone decides). Stops at the first break."""
            used, dedupe = 0, set()
            for files, why in items:
                if used >= limit:
                    break
                h = _hash(files)
                if h in dedupe:
                    continue
                dedupe.add(h)
                entry = "main.py" if "main.py" in files else next((p for p in files if p.endswith(".py")), "main.py")
                t = self._evaluate(rule, {"files": files, "entry_point": entry, "expected_fix": None,
                                          "why": why, "source": source}, seen, example_hashes)
                rd.twins.append(t)
                if t.verdict == "INVALID":
                    continue
                used += 1
                if t.verdict == "BREAK":
                    record_break(t)
                    return

        def examples_through(fn) -> list:
            return [m for ex in cand.examples[:2] for m in fn(ex["files"], ex["captures"])]

        if self.backend.mutate:                         # adversarial decoys first: they can break the rule
            run_batch(examples_through(self.backend.mutate), "mutation", self.max_mutations)
        if self.backend.controls and not rd.breaks:     # then benign controls: positive evidence only
            run_batch(examples_through(self.backend.controls), "control", self.max_controls)

        n_pass = lambda: sum(t.verdict == "PASS" for t in rd.twins)
        llm_valid = 0
        for _ in range(self.gen_rounds if not rd.breaks and n_pass() < self.min_pass else 0):
            reply = self.llm(SYSTEM, [{"role": "user", "content": self._gen_prompt(rule, cand, feedback)}],
                             json_mode=True)
            rd.gen_replies.append(reply[:4000])
            try:
                items = _parse_json(reply).get("twins", [])
            except (ValueError, AttributeError):
                items, feedback = [], feedback + ["reply was not valid JSON with a 'twins' list"]
            for raw in items if isinstance(items, list) else []:
                if llm_valid >= self.n_twins:
                    break
                t = self._evaluate(rule, raw, seen, example_hashes)
                rd.twins.append(t)
                if t.verdict == "INVALID":
                    feedback.append(f"files {sorted(t.files)}: {t.reason}"[:220])
                    continue
                llm_valid += 1
                if t.verdict == "BREAK":
                    record_break(t)
            if rd.breaks or n_pass() >= self.min_pass or llm_valid >= self.n_twins:
                break

        passes = sum(t.verdict == "PASS" for t in rd.twins)
        if rd.breaks:
            rd.verdict, rd.reason = "broken", f"{len(rd.breaks)} confirmed counterexample(s)"
        elif passes >= self.min_pass:
            rd.verdict, rd.reason = "passed", f"{passes} conclusive passes, 0 breaks"
        else:
            rd.verdict = "inconclusive"
            rd.reason = f"only {passes} conclusive pass(es) (need {self.min_pass}); uncertainty defers"
        return rd

    # ---------- audit + repair loop ----------
    def run(self, rule: Rule, cand: Candidate, compiler: Compiler, regression: RegressionSuite | None = None,
            max_repairs: int = 2) -> AuditReport:
        initial, cur, repairs = rule, rule, 0
        rnd = self.audit_round(cur, cand, regression)
        rounds = [rnd]
        notes: list[str] = []
        while rnd.verdict == "broken" and repairs < max_repairs:
            repairs += 1
            new, _ = compiler.repair(cur, cand, rnd.breaks)
            if new is None:
                notes.append(f"repair {repairs}: Compiler produced no valid rule")
                continue
            if not all(new.matches(cand.signature, e["captures"], e["files"]) for e in cand.examples):
                notes.append(f"repair {repairs}: rejected, no longer covers the original examples")
                continue
            cur = new
            rnd = self.audit_round(cur, cand, regression)
            rounds.append(rnd)
        final = "passed" if rnd.verdict == "passed" else "deferred"
        reason = rnd.reason + ("; " + "; ".join(notes) if notes else "")
        return AuditReport(rule.rule_id, rule.signature, final, reason, initial.model_dump(),
                           cur.model_dump(), repairs, rounds)


def candidate_from_rule(rule: Rule, runs: list[dict]) -> Candidate:
    """Rebuild the Candidate a rule was drafted from, using the Solver log."""
    by_id = {r["task_id"]: r for r in runs}
    examples = []
    for tid in rule.supporting_task_ids:
        r = by_id.get(tid)
        if r:
            _, caps = signature_of(r.get("traceback"))
            fix = r["solution"] if isinstance(r["solution"], dict) else json.loads(r["solution"])
            examples.append({"task_id": tid, "captures": caps, "files": r.get("files", {}), "fix": fix})
    return Candidate(rule.signature, rule.action, examples)