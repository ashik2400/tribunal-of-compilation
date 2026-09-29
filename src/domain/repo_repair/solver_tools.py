"""Solver-facing side of the repo-repair domain: investigation tools, an executable verifier,
and a ground-truth matcher. Kept out of generator.py so the generator stays untouched.

Design notes
- The Solver's answer is a *declarative fix* (a dict), matching GroundTruthFix.action's shape:
    {"type": "pip_install", "package": "<name>"}
    {"type": "fix_import", "replace_line": "<exact existing line>", "with_line": "<new line>"}
- pip_install is SIMULATED. A real install would pollute your venv (breaking later task generation)
  and, worse, an installed `colorutils` would shadow utils/colorutils.py and make the WRONG fix pass.
- verify_fix() is the in-loop signal (apply fix in a scratch copy, run entry point, exit code 0).
  It never reads ground truth. is_correct() is for the eval harness only.
"""
from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from src.solver import Tool

INSTRUCTIONS = """Investigate with the tools, then finish with a "final" whose "solution" is a JSON OBJECT
(not prose, not a shell command) of exactly one of these forms:
  {"type": "pip_install", "package": "<package name>"}
  {"type": "fix_import", "replace_line": "<exact existing line>", "with_line": "<one import statement>"}
fix_import only rewrites an import line into another import statement; you cannot define code inline.
Choose based on evidence in the repo (and pip_index), not just on the error message."""

# Simulated package index (stands in for PyPI). Lower-case names; pip_install of anything else fails,
# like real pip. Deliberately contains `colorutils`, which is a genuine PyPI package.
SIMULATED_INDEX = {"colorutils": "0.3.1", "requests": "2.32.3", "numpy": "2.1.0", "pillow": "10.4.0",
                   "colorama": "0.4.6", "pyyaml": "6.0.2"}


def _index_lookup(name: str) -> str | None:
    return SIMULATED_INDEX.get(str(name).strip().lower().replace("_", "-"))


def render_task(task) -> str:
    return (f"Running `python {task.entry_point}` fails with:\n{task.traceback}\n"
            "Propose a fix.")


def _write_repo(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def _run(root: Path, entry_point: str, pythonpath: Path | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    if pythonpath:
        env["PYTHONPATH"] = str(pythonpath) + os.pathsep + env.get("PYTHONPATH", "")
    r = subprocess.run([sys.executable, entry_point], cwd=root, capture_output=True,
                       text=True, timeout=10, env=env)
    return r.returncode, (r.stdout + r.stderr).strip()


class RepoSandbox:
    """One scratch copy of the task's repo, exposed to the Solver as read-only investigation tools."""

    def __init__(self, task):
        self.task = task
        self.root = Path(tempfile.mkdtemp(prefix="solver_"))
        _write_repo(self.root, task.files)

    def close(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _list_files(self) -> str:
        return "\n".join(sorted(str(p.relative_to(self.root)).replace("\\", "/")
                                for p in self.root.rglob("*") if p.is_file()))

    def _read_file(self, path: str) -> str:
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root.resolve()):
            return "ERROR: path escapes the repo"
        return p.read_text() if p.is_file() else f"ERROR: no such file: {path}"

    def _grep(self, pattern: str) -> str:
        hits = [f"{rel}:{i}: {line}"
                for rel, src in self.task.files.items()
                for i, line in enumerate(src.splitlines(), 1) if pattern in line]
        return "\n".join(hits) or "no matches"

    def _run_entry_point(self) -> str:
        code, out = _run(self.root, self.task.entry_point)
        return f"exit code {code}\n{out}"

    def _pip_index(self, package: str) -> str:
        v = _index_lookup(package)
        return f"{package} {v} is available on the package index" if v else f"{package}: not found on the package index"

    def tools(self) -> dict[str, Tool]:
        return {t.name: t for t in [
            Tool("list_files", "list_files() - list every file in the repo", self._list_files),
            Tool("read_file", "read_file(path) - return a file's contents", self._read_file),
            Tool("grep", "grep(pattern) - find lines containing pattern across the repo", self._grep),
            Tool("run_entry_point", "run_entry_point() - run the failing entry point, show output",
                 self._run_entry_point),
            Tool("pip_index", "pip_index(package) - check whether a package exists on the package index",
                 self._pip_index),
        ]}


def _as_fix(solution: Any) -> dict | None:
    if isinstance(solution, str):
        try:
            solution = json.loads(solution)
        except ValueError:
            return None
    return solution if isinstance(solution, dict) else None


def validate_fix(solution: Any) -> str | None:
    """Format gate. Returns an error message the model can act on, or None if well-formed."""
    fix = _as_fix(solution)
    if fix is None:
        return 'solution must be a JSON object such as {"type": "pip_install", "package": "..."}, not prose or a command.'
    kind = fix.get("type")
    if kind == "pip_install":
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", str(fix.get("package", ""))):
            return "pip_install needs a valid \"package\" name."
        return None
    if kind == "fix_import":
        old, new = fix.get("replace_line"), fix.get("with_line")
        if not isinstance(old, str) or not old.strip() or not isinstance(new, str):
            return "fix_import needs string \"replace_line\" and \"with_line\"."
        try:
            body = ast.parse(new.strip()).body
        except SyntaxError:
            body = []
        if len(body) != 1 or not isinstance(body[0], (ast.Import, ast.ImportFrom)):
            return ("fix_import.with_line must be exactly one import statement; you cannot define code "
                    "inline. If a third-party package is what's missing, use pip_install.")
        return None
    return 'unknown fix type; use "pip_install" or "fix_import".'


def verify_fix(task, solution: Any) -> bool:
    return verify_fix_explained(task, solution)[0]


def verify_fix_explained(task, solution: Any) -> tuple[bool, str]:
    """Apply the proposed fix in a scratch copy and check the entry point now exits 0.
    Returns (ok, reason) so Reflexion learns from the REAL failure, not a guess."""
    fix = _as_fix(solution)
    if not fix:
        return False, "answer is not a fix object"
    err = validate_fix(fix)
    if err:
        return False, err
    tmp = Path(tempfile.mkdtemp(prefix="verify_"))
    try:
        repo, site = tmp / "repo", tmp / "site"
        _write_repo(repo, task.files)
        site.mkdir()
        kind = fix.get("type")
        if kind == "pip_install":
            pkg = str(fix.get("package", ""))
            if _index_lookup(pkg) is None:
                return False, f"pip could not find package {pkg!r} on the index"
            module = pkg.lower().replace("-", "_").split(".")[0]
            # simulated package: importable, every attribute is a harmless callable
            (site / f"{module}.py").write_text("def __getattr__(name):\n    return lambda *a, **k: None\n")
        elif kind == "fix_import":
            old, new = str(fix.get("replace_line", "")).strip(), str(fix.get("with_line", ""))
            found = False
            for rel in task.files:
                p = repo / rel
                lines = p.read_text().splitlines()
                for i, line in enumerate(lines):
                    if old and line.strip() == old:
                        lines[i] = line[: len(line) - len(line.lstrip())] + new.strip()
                        found = True
                p.write_text("\n".join(lines) + "\n")
            if not found:
                return False, f"replace_line {old!r} does not appear in any file"
        else:
            return False, "unknown fix type"
        code, out = _run(repo, task.entry_point, pythonpath=site)
        if code == 0:
            return True, ""
        return False, "the entry point still fails after the fix: " + (out.splitlines() or ["?"])[-1]
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"could not run the fix: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def is_correct(task, solution: Any) -> bool:
    """Eval-harness only. Does the fix match the ground-truth *decision* (type + package)?
    Looser than the generator's dict equality, which would reject a valid with_line phrasing."""
    fix, gt = _as_fix(solution), task.ground_truth.action
    if not fix or fix.get("type") != gt["type"]:
        return False
    return fix.get("package") == gt.get("package") if gt["type"] == "pip_install" else True