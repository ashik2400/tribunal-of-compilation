"""
Domain adapter interface.

This is the seam that keeps the whole system (Solver, Compiler, Auditor,
Router) domain-independent by construction. None of those components are
ever allowed to import anything from `domain/repo_repair/` directly — they
only ever talk to this interface. Swapping in a new domain later (e.g. the
MLOps-domain stretch goal) means writing one new class that implements
these three methods, not touching the core pipeline at all.

Every concrete domain (starting with repo_repair) must subclass this and
implement all three methods.
"""

from abc import ABC, abstractmethod
from typing import Any


class DomainAdapter(ABC):
    """Contract every pluggable domain must satisfy."""

    @abstractmethod
    def generate_task(self, bug_family: str | None = None) -> Any:
        """
        Produce one task instance.

        If `bug_family` is given, generate a task belonging to that specific
        family (used by the Auditor to generate twin cases against an
        existing rule's claimed scope). If omitted, the generator picks a
        family itself (used for normal task-stream generation).

        Returns a domain-specific Task object (for repo_repair: the
        RepoRepairTask defined in domain/repo_repair/schema.py).
        """
        raise NotImplementedError

    @abstractmethod
    def check_ground_truth(self, task: Any, proposed_fix: Any) -> bool:
        """
        Given a task and a proposed fix (from the Solver, or from a
        Compiler-drafted rule being tested), return True if the fix is
        correct, False otherwise.

        This is the only place "correctness" is decided — the Solver,
        Compiler and Auditor never judge correctness themselves, they all
        call this.
        """
        raise NotImplementedError

    @abstractmethod
    def tools(self) -> list:
        """
        Return the list of tools the Solver's agentic loop is allowed to
        call for this domain (e.g. run_tests, read_file, install_package
        for repo_repair). Kept here rather than hardcoded in the Solver so
        a new domain can hand the Solver a completely different toolset
        without changing the Solver's code.
        """
        raise NotImplementedError
