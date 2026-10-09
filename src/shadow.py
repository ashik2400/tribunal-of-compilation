"""Shadow-checks: catch rules that were wrong but looked fine.

The Router answers by rule with no oversight, so a falsely-compiled rule fails SILENTLY. This module
re-runs a deterministic sample of rule-handled decisions through the Solver (k unanimous runs, as in the Auditor)
and compares the DECISIONS:
    agree         the Solver independently reaches the rule's decision
    diverge       the Solver unanimously reaches a different, verified decision  <- confirmed misfire
    inconclusive  the Solver was not unanimous / not verified: counted separately, never held against the rule
On `demote_after` confirmed divergences the rule is demoted (production -> demoted, pattern re-queued) and the
diverging repo is stored as a permanent regression twin, so any future version of the rule must handle it and the
CI gate fails if a rule that misfires on it is ever promoted again.
The shadow log is the data behind the false-compilation-rate curve.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

from src.auditor import AuditBackend, RegressionSuite, solver_consensus
from src.registry import Registry
from src.rules import signature_of


@dataclass
class ShadowOutcome:
    task_id: str
    rule_id: str
    outcome: str                      # agree | diverge | inconclusive | skipped
    reason: str = ""
    rule_fix: dict | None = None
    solver_fix: dict | None = None
    solver_runs: list = field(default_factory=list)
    demoted: bool = False


def sampled(seed: int, task_id: str, ts: float, rate: float) -> bool:
    """Deterministic sampling: the same decision is always in or out for a given seed, whatever else is in the log."""
    if rate >= 1:
        return True
    h = int(hashlib.sha1(f"{seed}:{task_id}:{ts}".encode()).hexdigest()[:8], 16)
    return h / 0xFFFFFFFF < rate


class ShadowChecker:
    def __init__(self, registry: Registry, backend: AuditBackend, regression: RegressionSuite | None = None,
                 k: int = 3, sample_rate: float = 0.2, demote_after: int = 1, seed: int = 0,
                 max_checks: int | None = None, log_path: str | Path | None = None,
                 clock: Callable[[], float] = time.time):
        self.registry, self.backend, self.regression = registry, backend, regression
        self.k, self.rate, self.demote_after, self.seed = k, sample_rate, demote_after, seed
        self.max_checks, self.clock = max_checks, clock
        self.log_path = Path(log_path) if log_path else None

    def _already_checked(self) -> set:
        if not self.log_path or not self.log_path.exists():
            return set()
        return {(r["decision_ts"], r["task_id"]) for r in
                (json.loads(l) for l in self.log_path.read_text().splitlines() if l.strip())}

    def run(self, records: list[dict]) -> list[ShadowOutcome]:
        done, out = self._already_checked(), []
        for rec in records:
            if self.max_checks is not None and len(out) >= self.max_checks:
                break
            if rec.get("handled_by") != "rule" or (rec["ts"], rec["task_id"]) in done:
                continue
            if not sampled(self.seed, rec["task_id"], rec["ts"], self.rate):
                continue
            entry = self.registry.entries.get(rec["rule_id"])
            if entry is None or entry.state != "production":
                continue                                  # already demoted (or unknown): nothing left to protect
            res = self._check(rec)
            out.append(res)
            self._log(rec, res)
        return out

    def _check(self, rec: dict) -> ShadowOutcome:
        rid = rec["rule_id"]
        res = ShadowOutcome(rec["task_id"], rid, "", rule_fix=rec["fix"])
        try:
            task = self.backend.build_task(rec["files"], rec.get("entry_point", "main.py"), f"shadow_{rec['task_id']}"[:60])
        except Exception as e:
            res.outcome, res.reason = "skipped", f"cannot replay this repo: {str(e)[:120]}"
            return res
        fix, res.solver_runs, why = solver_consensus(self.backend, task, self.k)
        if fix is None:
            res.outcome, res.reason = "inconclusive", why
        elif self.backend.decision_key(fix) == self.backend.decision_key(rec["fix"]):
            res.outcome, res.reason, res.solver_fix = "agree", "Solver independently reached the rule's decision", fix
        else:
            res.outcome, res.solver_fix = "diverge", fix
            res.reason = "Solver unanimously chose a different decision than the rule"
        self.registry.record_shadow(rid, res.outcome)
        if res.outcome == "diverge":
            if self.regression is not None:
                sig, _ = signature_of(rec["traceback"])
                self.regression.add({"signature": sig, "files": rec["files"], "entry_point": rec.get("entry_point", "main.py"),
                                     "correct_fix": fix, "why": "shadow-check divergence", "origin_rule": rid})
            entry = self.registry.entries[rid]
            if entry.stats["divergences"] >= self.demote_after:
                self.registry.demote(rid, f"shadow-check: {entry.stats['divergences']} confirmed divergence(s) "
                                          f"in {entry.stats['shadow_checks']} checks")
                res.demoted = True
        return res

    def _log(self, rec: dict, res: ShadowOutcome) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a") as f:
            f.write(json.dumps({"ts": self.clock(), "decision_ts": rec["ts"], **asdict(res)}) + "\n")


def divergence_curve(log_path: str | Path) -> list[dict]:
    """Cumulative shadow-check results over time: the false-compilation-rate curve."""
    rows, checks, div = [], 0, 0
    for line in Path(log_path).read_text().splitlines():
        r = json.loads(line)
        if r["outcome"] in ("agree", "diverge"):          # inconclusive/skipped carry no information either way
            checks += 1
            div += r["outcome"] == "diverge"
            rows.append({"ts": r["ts"], "checks": checks, "divergences": div, "rate": round(div / checks, 3)})
    return rows
