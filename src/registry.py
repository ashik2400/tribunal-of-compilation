"""Rule registry: the lifecycle of every compiled rule (the 'model registry' of the MLOps pipeline).

    (audit passed) --register--> STAGING --promote--> PRODUCTION --demote--> DEMOTED
    STAGING can also be demoted directly.

Fail-closed rules enforced here, not left to convention:
  * only a rule whose audit verdict is "passed" can be registered;
  * the rule must be byte-for-byte the audited version (fingerprint), and is re-checked at promotion,
    so a rule edited after its audit can never reach production;
  * promotion is blocked unless the rule is PROVABLY disjoint from every production rule on the same
    error signature that would decide differently (overlap you can't rule out is refused);
  * at runtime, if several production rules match and disagree, resolve() refuses to pick one.
Persistence is one human-readable JSON file written atomically; every transition is logged forever.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, Field

from src.rules import Rule, provably_disjoint

State = Literal["staging", "production", "demoted"]


class RegistryError(Exception):
    pass


class Event(BaseModel):
    ts: float
    from_state: str | None
    to_state: str
    reason: str = ""
    fingerprint: str


class Entry(BaseModel):
    rule: Rule
    state: State
    version: int = 1
    fingerprint: str
    audit: dict = Field(default_factory=dict)
    history: list[Event] = Field(default_factory=list)
    stats: dict = Field(default_factory=lambda: {"shadow_checks": 0, "divergences": 0})   # filled by shadow-checks


def fingerprint(rule: Rule) -> str:
    """Identity of what was audited: signature + scope + action. (Rationale text may change freely.)"""
    body = {"signature": rule.signature, "action": rule.action,
            "scope": [p.model_dump(exclude_none=True) for p in rule.scope]}
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]


def _audit_summary(report: dict) -> dict:
    twins: dict[str, int] = {}
    rounds = report.get("rounds") or []
    for t in (rounds[-1].get("twins", []) if rounds else []):
        key = f"{t.get('source', 'llm')}:{t.get('verdict')}"
        twins[key] = twins.get(key, 0) + 1
    return {"verdict": report.get("final_verdict"), "repairs_used": report.get("repairs_used", 0),
            "final_round_twins": twins, "reason": report.get("reason", "")}


class Registry:
    def __init__(self, path: str | Path | None = None,
                 same_decision: Callable[[dict, dict], bool] | None = None, clock: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.same_decision = same_decision or (lambda a, b: a == b)
        self.clock = clock
        self.entries: dict[str, Entry] = {}
        if self.path and self.path.exists():
            data = json.loads(self.path.read_text())
            self.entries = {k: Entry.model_validate(v) for k, v in data.get("entries", {}).items()}

    # ---------- persistence ----------
    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "entries": {k: json.loads(v.model_dump_json()) for k, v in self.entries.items()}}
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, self.path)                      # atomic: a crash never leaves a half-written registry

    def _log(self, entry: Entry, frm: str | None, to: str, reason: str) -> None:
        entry.state = to
        entry.history.append(Event(ts=self.clock(), from_state=frm, to_state=to, reason=reason,
                                   fingerprint=entry.fingerprint))

    # ---------- transitions ----------
    def register(self, rule: Rule, report: dict) -> Entry:
        if report.get("final_verdict") != "passed":
            raise RegistryError(f"{rule.rule_id}: audit verdict is {report.get('final_verdict')!r}, not 'passed'")
        audited = Rule.model_validate(report["final_rule"])
        fp = fingerprint(rule)
        if fingerprint(audited) != fp:
            raise RegistryError(f"{rule.rule_id}: rule differs from the version that was audited")
        existing = self.entries.get(rule.rule_id)
        if existing and existing.state != "demoted":
            if existing.fingerprint == fp:
                return existing                          # idempotent
            raise RegistryError(f"{rule.rule_id}: a different version is already {existing.state}; demote it first")
        if existing:                                     # demoted earlier: this is a new version of the rule
            existing.version += 1
            existing.rule, existing.fingerprint, existing.audit = rule, fp, _audit_summary(report)
            self._log(existing, "demoted", "staging", f"re-registered as version {existing.version}")
            self._save()
            return existing
        entry = Entry(rule=rule, state="staging", fingerprint=fp, audit=_audit_summary(report))
        entry.history.append(Event(ts=self.clock(), from_state=None, to_state="staging",
                                   reason="audit passed", fingerprint=fp))
        self.entries[rule.rule_id] = entry
        self._save()
        return entry

    def conflicts(self, rule: Rule) -> list[str]:
        """Production rules that might fire together with `rule` yet decide differently."""
        out = []
        for e in self.entries.values():
            if e.state == "production" and e.rule.rule_id != rule.rule_id and not provably_disjoint(e.rule, rule):
                if not self.same_decision(e.rule.action, rule.action):
                    out.append(e.rule.rule_id)
        return out

    def promote(self, rule_id: str) -> Entry:
        e = self._get(rule_id)
        if e.state != "staging":
            raise RegistryError(f"{rule_id}: only staging rules can be promoted (is {e.state})")
        if fingerprint(e.rule) != e.fingerprint:
            raise RegistryError(f"{rule_id}: rule was modified after its audit")
        bad = self.conflicts(e.rule)
        if bad:
            raise RegistryError(f"{rule_id}: not provably disjoint from production rule(s) {bad} that decide "
                                "differently; narrow the scopes (or demote one) before promoting")
        self._log(e, "staging", "production", "cleared audit and conflict check")
        self._save()
        return e

    def demote(self, rule_id: str, reason: str) -> Entry:
        e = self._get(rule_id)
        if not reason.strip():
            raise RegistryError("a demotion needs a reason")
        if e.state == "demoted":
            raise RegistryError(f"{rule_id} is already demoted")
        self._log(e, e.state, "demoted", reason)
        self._save()
        return e

    # ---------- runtime view (used by the Router) ----------
    def production_rules(self) -> list[Rule]:
        return [e.rule for e in self.entries.values() if e.state == "production"]

    def match(self, signature: str, captures: list[str], files: dict) -> list[Rule]:
        return [r for r in self.production_rules() if r.matches(signature, captures, files)]

    def resolve(self, signature: str, captures: list[str], files: dict) -> tuple[Rule | None, str]:
        """The single rule to apply, or (None, why). Never guesses between disagreeing rules."""
        hits = self.match(signature, captures, files)
        if not hits:
            return None, "no production rule matches"
        for other in hits[1:]:
            if not self.same_decision(hits[0].action, other.action):
                return None, f"ambiguous: {hits[0].rule_id} and {other.rule_id} disagree"
        return hits[0], "ok"

    def requeue_signatures(self) -> list[str]:
        """Signatures of demoted rules: patterns the Compiler should look at again."""
        return sorted({e.rule.signature for e in self.entries.values() if e.state == "demoted"})

    def _get(self, rule_id: str) -> Entry:
        if rule_id not in self.entries:
            raise RegistryError(f"unknown rule {rule_id}")
        return self.entries[rule_id]
