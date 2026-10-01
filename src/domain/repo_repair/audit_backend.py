"""repo_repair implementation of the Auditor's AuditBackend.

Twin repos are written by an LLM, then EXECUTED. So they are screened first (static checks) and run
with a timeout in a throwaway directory. This is a screen, not a jail: for hard isolation the audit
should run inside the Docker container planned for the packaging milestone."""
from __future__ import annotations

import ast
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable

from src.auditor import AuditBackend
from src.domain.repo_repair.schema import GroundTruthFix, RepoRepairTask
from src.domain.repo_repair.solver_tools import (INSTRUCTIONS, SIMULATED_INDEX, RepoSandbox, _as_fix, _run, _write_repo,
                                                 render_task, validate_fix, verify_fix_explained)
from src.solver import Solver

BLOCKED_MODULES = {"os", "sys", "subprocess", "shutil", "socket", "ctypes", "pathlib", "urllib", "http",
                   "ftplib", "smtplib", "multiprocessing", "threading", "importlib", "builtins", "pickle",
                   "marshal", "signal", "tempfile", "glob", "asyncio", "webbrowser"}
BLOCKED_CALLS = {"exec", "eval", "compile", "open", "__import__", "input", "exit", "quit", "globals", "locals"}
MAX_FILES, MAX_CHARS = 6, 1500


def screen_files(files: dict[str, str]) -> str | None:
    """Return a reason to refuse to execute these files, or None."""
    if len(files) > MAX_FILES:
        return f"too many files (max {MAX_FILES})"
    for path, text in files.items():
        norm = path.replace("\\", "/")
        if norm.startswith("/") or ".." in norm.split("/") or ":" in norm:
            return f"unsafe path {path!r}"
        if len(text) > MAX_CHARS:
            return f"{path} is too long (max {MAX_CHARS} chars)"
        if not norm.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                names = []
            for n in names:
                if n in BLOCKED_MODULES:
                    return f"{path} imports blocked module {n!r}"
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in BLOCKED_CALLS:
                return f"{path} calls blocked builtin {node.func.id!r}"
    return None


def build_task(files: dict[str, str], entry_point: str, task_id: str) -> RepoRepairTask:
    """Screen, run the entry point in a temp dir, and wrap the result as a task. Raises if unsafe/not failing."""
    reason = screen_files(files)
    if reason:
        raise ValueError(f"refused to run: {reason}")
    tmp = Path(tempfile.mkdtemp(prefix="twin_"))
    try:
        _write_repo(tmp, files)
        code, out = _run(tmp, entry_point)
    except subprocess.TimeoutExpired:
        raise ValueError("entry point timed out")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if code == 0:
        raise ValueError("the entry point ran successfully (exit 0), so the import problem was already satisfied; the repo must FAIL")
    return RepoRepairTask(task_id=task_id, bug_family="audit_twin", variant="audit_twin", files=files,
                          entry_point=entry_point, traceback=out,
                          ground_truth=GroundTruthFix(description="unlabelled auditor twin",
                                                      action={"type": "unknown"}))


DECOY_DIRS = ["utils", "lib", "helpers"]


def mutate_repo(files: dict[str, str], captures: list[str]) -> list[tuple[dict, str]]:
    """Deterministic twin operators (no LLM). The error says module X is missing; for each decoy
    location, add a local module X that exists but is NOT on Python's import path from the entry point.
    The error stays identical, the repo is still in most rules' scope, yet the right fix may now differ."""
    out: list[tuple[dict, str]] = []
    if not captures or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", captures[0]):
        return out
    cap = captures[0]
    if any(p == f"{cap}.py" or p.endswith(f"/{cap}.py") or f"/{cap}/" in f"/{p}" for p in files):
        return out        # a local module already exists: adding a second would make the right answer ambiguous
    code = "\n".join(files.values())
    attrs = sorted(set(re.findall(rf"\b{re.escape(cap)}\.([A-Za-z_]\w*)", code)))
    stub = "".join(f"def {a}(*args, **kwargs):\n    return None\n\n" for a in attrs) or "VALUE = 1\n"
    for d in DECOY_DIRS:
        path = f"{d}/{cap}.py"
        if path not in files:
            out.append(({**files, path: stub}, f"a local {path} exists in a subdirectory"))
    pkg = f"vendor/{cap}/__init__.py"
    if pkg not in files:
        out.append(({**files, pkg: stub}, f"a local package vendor/{cap}/ exists"))
    return out


def control_repo(files: dict[str, str], captures: list[str]) -> list[tuple[dict, str]]:
    """Benign perturbations that must NOT change the right answer. If a rule or the Solver is thrown by
    these, it is fragile. They give conclusive PASS evidence; they cannot catch a bad rule by themselves."""
    taken = {captures[0]} if captures else set()
    out: list[tuple[dict, str]] = []
    for path, text, why in [("config.py", "DEBUG = False\n", "an unrelated config.py is added"),
                            ("tools.py", "def helper(x):\n    return x\n", "an unrelated helper module is added"),
                            ("notes.txt", "release notes\n", "an unrelated notes file is added")]:
        if path not in files and path.split(".")[0] not in taken:
            out.append(({**files, path: text}, why))
    entry = "main.py" if "main.py" in files else None
    if entry and not files[entry].startswith("# "):
        out.append(({**files, entry: "# entry point\n" + files[entry]}, "a comment header is added"))
    return out


def decision_key(fix: dict):
    f = _as_fix(fix) or {}
    if f.get("type") == "pip_install":
        return ("pip_install", str(f.get("package", "")).lower().replace("_", "-"))
    return (f.get("type"),)


NOTES = ("Fix vocabulary (expected_fix must be exactly one of these):\n"
         '  {"type": "pip_install", "package": "<name>"}\n'
         '  {"type": "fix_import", "replace_line": "<exact existing line>", "with_line": "<one import statement>"}\n'
         "Facts about the test environment:\n"
         "- Each repo is run as `python <entry_point>` from the repo root and MUST fail with the same "
         "ModuleNotFoundError. A module Python can already import would not fail, so such a repo is rejected.\n"
         "- expected_fix is executed to check it really fixes the repo; a repo with any other error is rejected.\n"
         "- pip_install only works for packages on the package index, which contains exactly: "
         + ", ".join(sorted(SIMULATED_INDEX)) + ".\n"
         "- Repos are tiny plain Python (no os/sys/subprocess/network/file access).")


def make_backend(llm=None, solve_fn: Callable | None = None, solver_attempts: int = 2,
                 mutations: bool = False, controls: bool = False) -> AuditBackend:
    """solve_fn lets tests inject a scripted oracle instead of calling a real LLM."""
    def solve(task):
        sb = RepoSandbox(task)
        try:
            res = Solver(llm, sb.tools(), verify_fix_explained, instructions=INSTRUCTIONS,
                         render_task=render_task, validate=validate_fix,
                         max_attempts=solver_attempts).solve(task)
        finally:
            sb.close()
        fix = _as_fix(res.solution)
        return fix, bool(fix and res.verified)

    return AuditBackend(build_task=build_task,
                        verify=lambda t, fix: verify_fix_explained(t, fix),
                        solve=solve_fn or solve, decision_key=decision_key,
                        validate_fix=validate_fix, notes=NOTES,
                        mutate=mutate_repo if mutations else None,
                        controls=control_repo if controls else None)