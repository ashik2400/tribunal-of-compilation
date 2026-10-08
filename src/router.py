"""Router: send each failure to a PRODUCTION rule if one safely applies, else to the Solver.

A rule hit costs zero LLM calls. Two fail-closed safeguards:
  * the registry only returns a rule when exactly one decision is on the table (else: Solver);
  * the rule's fix is executed in the sandbox before it is returned; if it does not run, the Router falls
    back to the Solver and records which rule failed (a demotion signal for the shadow-check loop).
Every routing decision is logged with the repo, so shadow-checks can later re-run a sample through the Solver.
Note: executing the fix can only catch fixes that CRASH. A fix that runs but is wrong (the false-compilation
case) is invisible here by design; that is what the Auditor and shadow-checks are for.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from src.registry import Registry
from src.rules import signature_of


@dataclass
class RouteResult:
    task_id: str
    handled_by: str                  # "rule" | "solver"
    fix: Any
    verified: bool | None            # None = not checked
    reason: str
    rule_id: str | None = None
    llm_calls: int = 0
    failed_rule_id: str | None = None    # a rule matched but its fix did not run


class Router:
    def __init__(self, registry: Registry, solver_fn: Callable[[Any], tuple[Any, bool, int]],
                 verify: Callable[[Any, dict], Any] | None = None, log_path: str | Path | None = None,
                 clock: Callable[[], float] = time.time):
        """solver_fn(task) -> (fix, verified, llm_calls). verify(task, fix) -> bool or (bool, reason)."""
        self.registry, self.solver_fn, self.verify = registry, solver_fn, verify
        self.log_path, self.clock = Path(log_path) if log_path else None, clock

    def route(self, task: Any) -> RouteResult:
        sig, caps = signature_of(task.traceback)
        rule, why = self.registry.resolve(sig, caps, task.files)
        failed = None
        if rule:
            fix = rule.fix(caps)
            ok, vwhy = True, ""
            if self.verify:
                out = self.verify(task, fix)
                ok, vwhy = out if isinstance(out, tuple) else (bool(out), "")
            if ok:
                res = RouteResult(task.task_id, "rule", fix, True if self.verify else None,
                                  f"rule {rule.rule_id}", rule_id=rule.rule_id)
                self._log(task, res)
                return res
            failed, why = rule.rule_id, f"rule {rule.rule_id} matched but its fix did not run: {vwhy}"
        fix, verified, calls = self.solver_fn(task)
        res = RouteResult(task.task_id, "solver", fix, verified, why, llm_calls=calls, failed_rule_id=failed)
        self._log(task, res)
        return res

    def _log(self, task: Any, res: RouteResult) -> None:
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": self.clock(), **asdict(res), "traceback": task.traceback, "files": task.files}
        with self.log_path.open("a") as f:
            f.write(json.dumps(rec) + "\n")
