"""Builds the (lazy) Solver used when no rule matches. Kept apart from serve.py so scripts
can use it without importing the web framework."""
from __future__ import annotations

import os
from typing import Any

from src.domain.repo_repair.solver_tools import (INSTRUCTIONS, RepoSandbox, _as_fix, render_task, validate_fix,
                                                 verify_fix_explained)
from src.solver import Solver


def default_solver_factory():
    """Built lazily: the LLM is only needed (and its key only required) when no rule matches."""
    from src.llm import make_llm
    llm = make_llm({"provider": "groq", "model": os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b")})

    def solve(task) -> tuple[Any, bool, int]:
        sb = RepoSandbox(task)
        try:
            res = Solver(llm, sb.tools(), verify_fix_explained, instructions=INSTRUCTIONS, render_task=render_task,
                         validate=validate_fix).solve(task)
        finally:
            sb.close()
        calls = res.steps + len(res.reflections)
        return _as_fix(res.solution), bool(res.verified), calls
    return solve
