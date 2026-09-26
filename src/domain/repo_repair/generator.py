"""
The repo-repair domain adapter.

Implements the three-method contract from src/domain/base.py. This is the
only file in the whole project that knows what "colorutils", "pip_install",
or "ModuleNotFoundError" mean -- everything else (Solver, Compiler, Auditor,
Router, once they exist) only ever calls generate_task / check_ground_truth
/ tools and never looks inside this file.
"""

import random
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from src.domain.base import DomainAdapter
from src.domain.repo_repair.schema import GroundTruthFix, RepoRepairTask

BUG_FAMILY = "import_error_twin"


def _write_repo(tmp_dir: Path, files: dict[str, str]) -> None:
    for rel_path, content in files.items():
        full_path = tmp_dir / rel_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)


def _run_entry_point(tmp_dir: Path, entry_point: str) -> str:
    """
    Actually execute the entry point and capture the real traceback.

    Plain subprocess, no Docker -- see the explanation in chat: these are
    small synthetic files we generated ourselves, not untrusted code, so
    container isolation isn't earning its keep yet.
    """
    result = subprocess.run(
        [sys.executable, entry_point],
        cwd=tmp_dir,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stderr.strip()


class RepoRepairDomain(DomainAdapter):
    def generate_task(self, bug_family: str | None = None) -> RepoRepairTask:
        # Only one bug_family exists right now. This is deliberately the
        # seam where a second bug_family gets added later -- one more
        # `elif family == "..."` block, nothing else in the project changes.
        family = bug_family or BUG_FAMILY
        if family != BUG_FAMILY:
            raise ValueError(f"unknown bug_family: {family}")

        variant = random.choice(["missing_package", "needs_local_import"])
        task_id = f"{family}__{variant}__{uuid.uuid4().hex[:8]}"

        if variant == "missing_package":
            files = {
                "main.py": (
                    "import colorutils\n"
                    "print(colorutils.hex_to_rgb('#ffffff'))\n"
                )
            }
            ground_truth = GroundTruthFix(
                description=(
                    "colorutils is a genuine third-party package that is "
                    "simply not installed in this environment."
                ),
                action={"type": "pip_install", "package": "colorutils"},
            )
        else:  # needs_local_import
            files = {
                "utils/colorutils.py": (
                    "def hex_to_rgb(hex_str):\n"
                    "    hex_str = hex_str.lstrip('#')\n"
                    "    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))\n"
                ),
                "main.py": (
                    "import colorutils\n"
                    "print(colorutils.hex_to_rgb('#ffffff'))\n"
                ),
            }
            ground_truth = GroundTruthFix(
                description=(
                    "colorutils already exists locally under utils/; the "
                    "import statement is wrong, not the package availability."
                ),
                action={
                    "type": "fix_import",
                    "replace_line": "import colorutils",
                    "with_line": "from utils import colorutils",
                },
            )

        entry_point = "main.py"

        tmp_dir = Path(tempfile.mkdtemp(prefix="repo_repair_"))
        try:
            _write_repo(tmp_dir, files)
            traceback_text = _run_entry_point(tmp_dir, entry_point)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return RepoRepairTask(
            task_id=task_id,
            bug_family=family,
            variant=variant,
            files=files,
            entry_point=entry_point,
            traceback=traceback_text,
            ground_truth=ground_truth,
        )

    def check_ground_truth(self, task: RepoRepairTask, proposed_fix: dict) -> bool:
        """
        Placeholder for Milestone 2: a direct dict comparison against the
        ground-truth action. Deliberately naive -- once the Solver exists
        and produces real, executable fixes, this will be replaced with
        "apply the fix, re-run entry_point, check it now succeeds" instead
        of comparing dicts. Kept simple so generator + schema can be tested
        end-to-end before the Solver exists.
        """
        return proposed_fix == task.ground_truth.action

    def tools(self) -> list[str]:
        """
        Names only for now. These become real callable tools once the
        Solver's agentic loop is built (the milestone after this one).
        """
        return ["read_file", "list_files", "run_python", "pip_install", "edit_file"]
