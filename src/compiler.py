"""Compiler: turns a streak of consistent solves into a draft deterministic rule.

Two deliberately separate stages:
  find_candidates  - pure bookkeeping, no LLM. Fires after N consecutive verified, high-confidence
                     solves of the same failure signature that all chose the same fix *shape*.
  Compiler.draft   - ONE LLM call. The fix shape is already fixed by the evidence; the model only
                     proposes the rule's SCOPE (when is it safe?) and a rationale.

Ground truth is never read here (records' `correct` field is ignored): the Compiler only knows
what the Solver and the in-sandbox verifier told it, exactly as it would in production.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from src.llm import LLM
from src.rules import Rule, fill_any, signature_of
from src.solver import _parse_json


def _fix_dict(solution: Any) -> dict | None:
    if isinstance(solution, str):
        try:
            solution = json.loads(solution)
        except ValueError:
            return None
    return solution if isinstance(solution, dict) else None


def abstract_fix(fix: dict, captures: list[str]) -> dict:
    """Replace captured names inside a fix by {0},{1}.. so fixes for different modules share a shape."""
    def walk(v):
        if isinstance(v, str):
            for i, c in enumerate(captures):
                if c:
                    v = re.sub(rf"\b{re.escape(c)}\b", f"{{{i}}}", v)
            return v
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        return v
    return walk(fix)


@dataclass
class Candidate:
    signature: str
    shape: dict                           # abstracted fix template
    examples: list[dict] = field(default_factory=list)   # task_id, captures, files, fix
    @property
    def n(self) -> int:
        return len(self.examples)


def find_candidates(runs: list[dict], n_required: int = 3, min_conf: float = 0.9,
                    keep_examples: int = 5) -> list[Candidate]:
    """Replay the log in time order, like a live system would. Per signature, a candidate is emitted
    the moment a streak of identical-shape verified, high-confidence solves first reaches n_required
    (once per streak). Any unverified/low-confidence solve, or a solve with a different fix shape,
    resets that signature's streak - but a candidate already emitted stays emitted."""
    streaks: dict[str, list[tuple[dict, dict]]] = {}
    out: list[Candidate] = []
    for run in sorted(runs, key=lambda r: r.get("ts", 0)):
        sig, caps = signature_of(run.get("traceback"))
        fix = _fix_dict(run.get("solution"))
        if not run.get("verified") or run.get("confidence", 0) < min_conf or fix is None:
            streaks[sig] = []
            continue
        shape = abstract_fix(fix, caps)
        ex = {"task_id": run["task_id"], "captures": caps, "files": run.get("files", {}), "fix": fix}
        cur = streaks.setdefault(sig, [])
        if cur and json.dumps(cur[-1][0], sort_keys=True) != json.dumps(shape, sort_keys=True):
            cur.clear()
        cur.append((shape, ex))
        if len(cur) == n_required:
            out.append(Candidate(sig, shape, [e for _, e in cur][-keep_examples:]))
    return out


def dedupe_candidates(cands: list[Candidate]) -> list[Candidate]:
    """A live system already has the rule after the first streak; a later streak with the same
    (signature, fix shape) is not a new rule. Keep the first."""
    seen, out = set(), []
    for c in cands:
        key = (c.signature, json.dumps(c.shape, sort_keys=True))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


SYSTEM = """You are the Compiler in a system that turns repeated agent solutions into cheap deterministic rules.
A rule fires whenever a failure has the same signature AND every scope condition holds on the repo.
Your rule will run later with NO oversight, so the scope is the only thing protecting future cases that
merely look the same. Broader scope saves more cost; but a rule that fires wrongly is worse than no rule.

Reply with ONE JSON object and nothing else:
  {"scope": [{"op": "<op>", "glob": "<file glob>", "text": "<optional text>"}, ...],
   "rationale": "<one or two sentences>"}
Allowed ops: file_exists, file_absent, text_contains, text_absent (text only for the text_* ops).
Globs match repo paths; a leading "**/" also matches root files. {0}, {1}... in glob/text stand for the
quoted names in the error message. The fix template is already decided; do not change it."""


class Compiler:
    def __init__(self, llm: LLM, validate_action: Callable[[dict], str | None] | None = None,
                 max_file_chars: int = 400, attempts: int = 2):
        self.llm, self.validate_action = llm, validate_action
        self.max_file_chars, self.attempts = max_file_chars, attempts

    def _prompt(self, cand: Candidate) -> str:
        parts = [f"Failure signature: {cand.signature}",
                 "{0}, {1}... = the quoted names in the error message.",
                 f"Fix template the solver converged on: {json.dumps(cand.shape)}", ""]
        for i, ex in enumerate(cand.examples, 1):
            parts.append(f"Example {i}: quoted names = {ex['captures']}")
            for path, text in ex["files"].items():
                parts.append(f"  --- {path} ---\n  " + text[: self.max_file_chars].replace("\n", "\n  "))
            parts.append(f"  fix used: {json.dumps(ex['fix'])}\n")
        parts.append("Write the scope conditions under which this fix template is safe to apply automatically.")
        return "\n".join(parts)

    def draft(self, cand: Candidate) -> tuple[Rule | None, dict]:
        """Returns (rule or None, log). The log always carries prompt/replies/errors for tracking."""
        prompt = self._prompt(cand)
        return self._run(cand, prompt, [{"role": "user", "content": prompt}])

    def repair(self, rule: Rule, cand: Candidate, breaks: list[dict]) -> tuple[Rule | None, dict]:
        """Ask for a narrower scope after the Auditor found repos where the rule misfires."""
        prompt = self._prompt(cand)
        fb = ["The Auditor found repos where your rule fires but gives the WRONG fix."]
        for i, b in enumerate(breaks, 1):
            fb.append(f"Counterexample {i}:")
            for path, text in b["files"].items():
                fb.append(f"  --- {path} ---\n  " + text[: self.max_file_chars].replace("\n", "\n  "))
            fb.append(f"  rule's fix: {json.dumps(b['rule_fix'])}\n  correct fix: {json.dumps(b['correct_fix'])}")
        fb.append("Revise the scope so it EXCLUDES these cases (and similar ones) while still matching every "
                  "original example above. A scope that matches nothing is useless. Same JSON format.")
        prev = json.dumps({"scope": [p.model_dump(exclude_none=True) for p in rule.scope],
                           "rationale": rule.rationale})
        return self._run(cand, prompt, [{"role": "user", "content": prompt},
                                        {"role": "assistant", "content": prev},
                                        {"role": "user", "content": "\n".join(fb)}])

    def _run(self, cand: Candidate, prompt: str, messages: list[dict]) -> tuple[Rule | None, dict]:
        log: dict = {"signature": cand.signature, "prompt": prompt, "replies": [], "errors": []}
        for _ in range(self.attempts):
            reply = self.llm(SYSTEM, messages, json_mode=True)
            log["replies"].append(reply)
            try:
                data = _parse_json(reply)
                rule = Rule.model_validate({
                    "rule_id": "rule_" + hashlib.sha1(
                        (cand.signature + json.dumps(cand.shape, sort_keys=True)).encode()).hexdigest()[:8],
                    "signature": cand.signature, "scope": data.get("scope", []),
                    "action": cand.shape, "rationale": str(data.get("rationale", "")),
                    "supporting_task_ids": [e["task_id"] for e in cand.examples]})
                if self.validate_action:
                    for ex in cand.examples:
                        err = self.validate_action(fill_any(cand.shape, ex["captures"]))
                        if err:
                            raise ValueError(f"action template invalid: {err}")
                return rule, log
            except Exception as e:     # bad JSON, bad predicate op, invalid template -> ask again once
                log["errors"].append(str(e)[:300])
                messages = messages + [{"role": "assistant", "content": reply},
                                       {"role": "user", "content": f"That reply was invalid ({str(e)[:200]}). Reply again."}]
        return None, log