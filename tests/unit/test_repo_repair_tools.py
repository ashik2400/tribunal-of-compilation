import json
from src.domain.repo_repair.schema import GroundTruthFix, RepoRepairTask
from src.domain.repo_repair.solver_tools import RepoSandbox, is_correct, render_task, verify_fix
from src.solver import Solver

MAIN = "import colorutils\nprint(colorutils.hex_to_rgb('#ffffff'))\n"
LOCAL = ("def hex_to_rgb(h):\n    h = h.lstrip('#')\n"
         "    return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))\n")
TB = "ModuleNotFoundError: No module named 'colorutils'"
PIP = {"type": "pip_install", "package": "colorutils"}
IMP = {"type": "fix_import", "replace_line": "import colorutils", "with_line": "from utils import colorutils"}


def make(variant):
    if variant == "missing_package":
        files, gt = {"main.py": MAIN}, PIP
    else:
        files, gt = {"main.py": MAIN, "utils/colorutils.py": LOCAL}, IMP
    return RepoRepairTask(task_id=variant, bug_family="import_error_twin", variant=variant,
                          files=files, entry_point="main.py", traceback=TB,
                          ground_truth=GroundTruthFix(description="d", action=gt))


def test_correct_fixes_verify_and_match():
    for variant, fix in [("missing_package", PIP), ("needs_local_import", IMP)]:
        t = make(variant)
        assert verify_fix(t, fix) and is_correct(t, fix)


def test_twin_wrong_fix_is_caught_only_by_ground_truth():
    # pip_install on the local-import twin "works" when executed (shadowing) -> verifier is fooled,
    # ground truth is not. This gap is exactly why the project measures false compilation.
    t = make("needs_local_import")
    assert verify_fix(t, PIP) and not is_correct(t, PIP)


def test_wrong_import_fix_on_missing_package_fails_verify():
    t = make("missing_package")
    assert not verify_fix(t, IMP)          # replace_line exists, but utils/ doesn't -> still crashes


def test_verifier_rejects_garbage():
    t = make("missing_package")
    assert not verify_fix(t, "not json") and not verify_fix(t, {"type": "rm_rf"})
    assert not verify_fix(t, {"type": "pip_install", "package": "x; rm -rf /"})


def test_accepts_json_string_and_loose_with_line():
    t = make("needs_local_import")
    loose = {**IMP, "with_line": "from utils import colorutils  "}
    assert verify_fix(t, json.dumps(loose)) and is_correct(t, loose)


def test_sandbox_tools_and_traversal_guard():
    t = make("needs_local_import"); sb = RepoSandbox(t)
    try:
        tools = sb.tools()
        assert "utils/colorutils.py" in tools["list_files"].fn()
        assert "ModuleNotFoundError" in tools["run_entry_point"].fn()
        assert "escapes" in tools["read_file"].fn("../../etc/passwd")
    finally:
        sb.close()


def test_solver_end_to_end_with_scripted_llm():
    t = make("needs_local_import"); sb = RepoSandbox(t)
    replies = iter([json.dumps({"thought": "look", "action": "list_files", "args": {}}),
                    json.dumps({"thought": "local exists", "final": {"solution": IMP, "confidence": 0.9}})])
    try:
        r = Solver(lambda s, m, **kw: next(replies), sb.tools(), verify_fix,
                   render_task=render_task).solve(t)
    finally:
        sb.close()
    assert r.verified and is_correct(t, r.solution) and r.steps == 2


def test_inline_code_hack_is_rejected_as_a_fix_import():
    from src.domain.repo_repair.solver_tools import validate_fix, verify_fix_explained
    hack = {"type": "fix_import", "replace_line": "import colorutils",
            "with_line": "import types; colorutils = types.ModuleType('colorutils')"}
    assert "single import" in validate_fix(hack) or "exactly one import" in validate_fix(hack)
    ok, why = verify_fix_explained(make("missing_package"), hack)
    assert not ok and why


def test_prose_answer_is_a_format_error_not_a_reflection_trigger():
    from src.domain.repo_repair.solver_tools import validate_fix
    assert "JSON object" in validate_fix("pip install colorutils")


def test_simulated_index_behaves_like_pip():
    from src.domain.repo_repair.solver_tools import verify_fix_explained
    t = make("missing_package")
    assert verify_fix(t, {"type": "pip_install", "package": "ColorUtils"})     # case-insensitive
    ok, why = verify_fix_explained(t, {"type": "pip_install", "package": "definitely-not-real"})
    assert not ok and "could not find" in why
    sb = RepoSandbox(t)
    try:
        assert "available" in sb.tools()["pip_index"].fn("colorutils")
        assert "not found" in sb.tools()["pip_index"].fn("nope-pkg")
    finally:
        sb.close()