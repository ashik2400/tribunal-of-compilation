"""Generator: seed-driven determinism and machine-independent tracebacks."""
import tempfile
from pathlib import Path

import pytest

from src.domain.repo_repair.generator import REPO_PLACEHOLDER, VARIANTS, RepoRepairDomain, normalize_paths
from src.domain.repo_repair.solver_tools import is_correct, render_task, verify_fix
from src.rules import signature_of

D = RepoRepairDomain()


def test_same_seed_gives_identical_task():
    for seed in (0, 1, 7, 12345):
        assert D.generate_task(seed=seed).model_dump() == D.generate_task(seed=seed).model_dump()


def test_same_seed_and_variant_gives_identical_task():
    for v in VARIANTS:
        assert D.generate_task(seed=3, variant=v).model_dump() == D.generate_task(seed=3, variant=v).model_dump()


def test_different_seeds_give_different_ids():
    ids = [D.generate_task(seed=s).task_id for s in range(10)]
    assert len(set(ids)) == len(ids)
    assert D.generate_task(seed=4, variant=VARIANTS[0]).task_id != D.generate_task(seed=5, variant=VARIANTS[0]).task_id


def test_seed_alone_chooses_both_variants():
    assert {D.generate_task(seed=s).variant for s in range(20)} == set(VARIANTS)


def test_seed_is_required_and_unknown_variant_is_refused():
    with pytest.raises(TypeError):
        D.generate_task()
    with pytest.raises(ValueError):
        D.generate_task(seed=0, variant="nope")


def test_stored_traceback_has_no_machine_paths():
    tmp = tempfile.gettempdir()
    for v in VARIANTS:
        tb = D.generate_task(seed=0, variant=v).traceback
        assert f'File "{REPO_PLACEHOLDER}/main.py"' in tb
        assert tmp not in tb and "repo_repair_" not in tb and "\\" not in tb


def test_normalized_traceback_keeps_signature_solver_prompt_and_verifier_working():
    for v in VARIANTS:
        t = D.generate_task(seed=1, variant=v)
        sig, caps = signature_of(t.traceback)
        assert sig == "ModuleNotFoundError: No module named '{}'" and caps == ["colorutils"]
        assert t.traceback in render_task(t)
        assert verify_fix(t, t.ground_truth.action) and is_correct(t, t.ground_truth.action)


def test_normalize_paths_handles_windows_and_posix_separators():
    root = Path("C:/Users/x/AppData/Local/Temp/repo_repair_ab12")
    win = str(root).replace("/", "\\")
    text = (f'Traceback (most recent call last):\n  File "{win}\\main.py", line 1, in <module>\n'
            f'  File "{win}\\utils\\colorutils.py", line 2\nModuleNotFoundError: No module named \'colorutils\'')
    out = normalize_paths(text, Path(win))
    assert 'File "<repo>/main.py"' in out and 'File "<repo>/utils/colorutils.py"' in out
    assert "Temp" not in out
    assert out.splitlines()[-1] == "ModuleNotFoundError: No module named 'colorutils'"     # error line untouched
    posix = normalize_paths('File "/tmp/repo_repair_q/main.py", line 1', Path("/tmp/repo_repair_q"))
    assert posix == 'File "<repo>/main.py", line 1'
