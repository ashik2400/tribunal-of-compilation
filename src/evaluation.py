"""Ground-truth evaluation of rules, independent of the Auditor.
Applies a rule to a set of tasks and uses the answer key (via is_correct) to count how often it fires wrongly.
This is the false-compilation measurement; the Auditor's verdict is judged against it."""
from __future__ import annotations

from typing import Any, Callable

from src.rules import Rule, signature_of


def evaluate_rule(rule: Rule, tasks: list, is_correct: Callable[[Any, dict], bool]) -> dict:
    fires, correct, wrong = 0, 0, []
    for t in tasks:
        sig, caps = signature_of(t.traceback)
        if rule.matches(sig, caps, t.files):
            fires += 1
            if is_correct(t, rule.fix(caps)):
                correct += 1
            else:
                wrong.append(t.task_id)
    return {"tasks": len(tasks), "fires": fires, "correct_fires": correct, "false_fires": fires - correct,
            "false_fire_rate": round((fires - correct) / fires, 3) if fires else 0.0,
            "coverage": round(fires / len(tasks), 3) if tasks else 0.0, "wrong_task_ids": wrong}
