"""
The data contract for a repo-repair task.

Every task the generator produces gets constructed as a `RepoRepairTask`.
Pydantic validates the fields the moment the object is created — if the
generator ever produces something malformed (missing a file, an empty
traceback, a nonsense variant name), this raises immediately instead of
letting a broken task quietly enter data/processed/ and confuse the Solver
three components downstream. That immediate raise IS the "validation gate"
from the MLOps pipeline table — it happens here, at construction time,
not as a separate script run afterward.
"""

from pydantic import BaseModel, Field, field_validator


class GroundTruthFix(BaseModel):
    """The correct resolution for a task, used by check_ground_truth()."""

    # Human-readable explanation, useful for logging/debugging and for
    # the eventual eval writeup ("what SHOULD have happened here").
    description: str

    # Machine-checkable form of the fix. Kept generic (a dict) because
    # different bug families need different things here: an import-error
    # fix might just be {"action": "pip_install", "package": "colorutils"}
    # while a later bug family might need an actual code patch. The
    # Compiler and Auditor read this field's shape based on bug_family,
    # not a fixed schema — bug_family is the discriminator.
    action: dict


class RepoRepairTask(BaseModel):
    """One instance of a repo-repair task: a tiny buggy repo + how to fix it."""

    task_id: str

    # bug_family groups tasks that share the same *surface* traceback
    # shape. variant distinguishes tasks within a family that require
    # DIFFERENT correct fixes despite that shared surface — this pair is
    # what makes "twin cases" representable at all. Two tasks with the
    # same bug_family but different variant are twins by definition.
    bug_family: str
    variant: str

    # The mini-repo itself: filename -> file content. Small and synthetic
    # on purpose (no need for a real large repo to demonstrate a twin
    # case) — kept as plain text so no Docker/sandbox is required yet
    # just to GENERATE and VALIDATE a task. Actually *running* these
    # files to reproduce the traceback is a separate concern we'll wire
    # up next, once schema + generation are solid on their own.
    files: dict[str, str]

    # Which file/command reproduces the bug when run.
    entry_point: str

    # Populated once we actually execute entry_point (next step, not yet
    # in this file). Optional here because a task is still a *valid* task
    # the instant it's generated, before execution happens.
    traceback: str | None = None

    ground_truth: GroundTruthFix

    generator_version: str = "0.1.0"

    @field_validator("files")
    @classmethod
    def must_have_at_least_one_file(cls, v: dict[str, str]) -> dict[str, str]:
        if not v:
            raise ValueError("a task must contain at least one file")
        return v

    @field_validator("bug_family", "variant")
    @classmethod
    def must_not_be_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("bug_family/variant cannot be blank")
        return v
