"""Rule format shared by the Compiler, Auditor and (later) Router.

A Rule is deterministic data, not code: it fires when (a) the failure's *signature* matches and
(b) every scope predicate holds on the repo. Then it emits a fix built from a template.
Keeping rules as data means the Router can run them with zero LLM calls, the Auditor can
test them mechanically, and the registry can version them like any other artifact.
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Literal

from pydantic import BaseModel, Field


def signature_of(traceback: str | None) -> tuple[str, list[str]]:
    """Normalize a traceback to (signature key, captured quoted names).
    "ModuleNotFoundError: No module named 'colorutils'" -> ("ModuleNotFoundError: No module named '{}'", ["colorutils"])
    Only the last line matters: it is the *surface* that twin cases share."""
    lines = [l.strip() for l in (traceback or "").splitlines() if l.strip()]
    last = lines[-1] if lines else "unknown"
    return re.sub(r"'[^']*'", "'{}'", last), re.findall(r"'([^']*)'", last)


def fill(text: str, captures: list[str]) -> str:
    """Replace {0}, {1}... with captured names. Other braces (e.g. Python dict literals) are untouched."""
    def sub(m: re.Match) -> str:
        i = int(m.group(1))
        return captures[i] if i < len(captures) else m.group(0)
    return re.sub(r"\{(\d+)\}", sub, text)


def fill_any(value: Any, captures: list[str]) -> Any:
    if isinstance(value, str):
        return fill(value, captures)
    if isinstance(value, dict):
        return {k: fill_any(v, captures) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_any(v, captures) for v in value]
    return value


def _glob_match(path: str, glob: str) -> bool:
    path, glob = path.replace("\\", "/"), glob.replace("\\", "/")
    return fnmatch.fnmatch(path, glob) or (glob.startswith("**/") and fnmatch.fnmatch(path, glob[3:]))


class Predicate(BaseModel):
    """One machine-checkable condition on the repo's files."""
    op: Literal["file_exists", "file_absent", "text_contains", "text_absent"]
    glob: str
    text: str | None = None

    def holds(self, files: dict[str, str], captures: list[str]) -> bool:
        glob = fill(self.glob, captures)
        paths = [p for p in files if _glob_match(p, glob)]
        if self.op == "file_exists":
            return bool(paths)
        if self.op == "file_absent":
            return not paths
        needle = fill(self.text or "", captures)
        found = any(needle in files[p] for p in paths)
        return found if self.op == "text_contains" else not found


class Rule(BaseModel):
    rule_id: str
    signature: str                       # normalized traceback key this rule applies to
    scope: list[Predicate] = Field(default_factory=list)   # the Compiler's CLAIM of when it's safe
    action: dict                         # fix template; {0}.. filled from the error's quoted names
    rationale: str = ""
    supporting_task_ids: list[str] = Field(default_factory=list)

    def matches(self, signature: str, captures: list[str], files: dict[str, str]) -> bool:
        return signature == self.signature and all(p.holds(files, captures) for p in self.scope)

    def fix(self, captures: list[str]) -> dict:
        return fill_any(self.action, captures)


def provably_disjoint(a: "Rule", b: "Rule") -> bool:
    """True only if NO repo can satisfy both rules' scopes (so they can never both fire).
    Conservative: a False answer means 'could not prove it', not 'they overlap'.
    Proof patterns: different signatures; file_exists <concrete path> vs file_absent <glob matching it>;
    text_contains vs text_absent on the same glob and text."""
    if a.signature != b.signature:
        return True
    caps = [f"cap{i}" for i in range(10)]          # same placeholder substitution on both sides

    def clash(p: Predicate, q: Predicate) -> bool:
        gp, gq = fill(p.glob, caps), fill(q.glob, caps)
        if p.op == "file_exists" and q.op == "file_absent":
            return not any(ch in gp for ch in "*?[") and _glob_match(gp, gq)
        if p.op == "text_contains" and q.op == "text_absent":
            return gp == gq and fill(p.text or "", caps) == fill(q.text or "", caps)
        return False

    return any(clash(p, q) or clash(q, p) for p in a.scope for q in b.scope)
