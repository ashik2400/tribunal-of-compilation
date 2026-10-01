import json
from src.compiler import Compiler, dedupe_candidates, find_candidates
from src.domain.repo_repair.solver_tools import validate_fix
from src.rules import Rule

TB = "Traceback (most recent call last):\nModuleNotFoundError: No module named '{}'"
LOCAL = {"main.py": "import colorutils\n", "utils/colorutils.py": "def hex_to_rgb(h): ...\n"}
MISSING = {"main.py": "import colorutils\n"}


def run(i, kind, mod="colorutils", verified=True, conf=0.95):
    if kind == "pip":
        fix, files = {"type": "pip_install", "package": mod}, {"main.py": f"import {mod}\n"}
    else:
        fix = {"type": "fix_import", "replace_line": f"import {mod}", "with_line": f"from utils import {mod}"}
        files = {"main.py": f"import {mod}\n", f"utils/{mod}.py": "x = 1\n"}
    return {"ts": i, "task_id": f"t{i}", "traceback": TB.format(mod), "files": files, "solution": fix,
            "verified": verified, "confidence": conf, "correct": kind == "pip"}


def test_needs_n_consecutive_consistent_solves():
    assert find_candidates([run(1, "pip"), run(2, "pip")], n_required=3) == []
    c = find_candidates([run(i, "pip") for i in range(3)], n_required=3)
    assert len(c) == 1 and c[0].shape == {"type": "pip_install", "package": "{0}"}


def test_conflicting_fix_resets_streak():
    runs = [run(1, "pip"), run(2, "pip"), run(3, "imp"), run(4, "pip"), run(5, "pip")]
    assert find_candidates(runs, n_required=3) == []
    assert len(find_candidates(runs + [run(6, "pip")], n_required=3)) == 1


def test_streak_is_found_even_if_broken_later():
    # the live system would have compiled at run 3; a later twin must not erase that fact
    assert len(find_candidates([run(1, "pip"), run(2, "pip"), run(3, "pip"), run(4, "imp")], 3)) == 1
    two = find_candidates([run(i, "pip") for i in range(1, 4)] + [run(i, "imp") for i in range(4, 7)], 3)
    assert [c.shape["type"] for c in two] == ["pip_install", "fix_import"]   # same signature, two rival rules


def test_long_streak_emits_only_once():
    assert len(find_candidates([run(i, "pip") for i in range(1, 8)], 3)) == 1


def test_unverified_or_low_confidence_breaks_streak():
    runs = [run(1, "pip"), run(2, "pip", verified=False), run(3, "pip"), run(4, "pip")]
    assert find_candidates(runs, n_required=3) == []
    runs = [run(1, "pip"), run(2, "pip", conf=0.56), run(3, "pip"), run(4, "pip")]
    assert find_candidates(runs, n_required=3) == []


def test_shape_generalizes_across_module_names():
    c = find_candidates([run(1, "pip", "requests"), run(2, "pip", "numpy"), run(3, "pip", "pillow")], 3)
    assert len(c) == 1 and c[0].shape["package"] == "{0}"


def test_compiler_never_reads_ground_truth():
    # `correct` is False for every "imp" run here, but the Compiler must not care
    runs = [run(i, "imp") for i in range(3)]
    assert len(find_candidates(runs, 3)) == 1


def scripted(*replies):
    it = iter(replies)
    return lambda system, messages, **kw: next(it)


GOOD = json.dumps({"scope": [{"op": "file_absent", "glob": "**/{0}.py"}], "rationale": "no local module"})


def test_draft_builds_rule_that_fires_only_where_scoped():
    cand = find_candidates([run(i, "pip") for i in range(3)], 3)[0]
    rule, log = Compiler(scripted(GOOD), validate_action=validate_fix).draft(cand)
    assert isinstance(rule, Rule) and rule.action == cand.shape and len(rule.supporting_task_ids) == 3
    assert rule.matches(cand.signature, ["colorutils"], MISSING)
    assert not rule.matches(cand.signature, ["colorutils"], LOCAL)      # the twin is out of scope


def test_empty_scope_rule_fires_on_the_twin():
    # what a lazy Compiler would ship: this is the false-compilation risk the Auditor exists to catch
    cand = find_candidates([run(i, "pip") for i in range(3)], 3)[0]
    rule, _ = Compiler(scripted(json.dumps({"scope": [], "rationale": "always"}))).draft(cand)
    assert rule.matches(cand.signature, ["colorutils"], MISSING) and rule.matches(cand.signature, ["colorutils"], LOCAL)


def test_draft_retries_once_on_bad_reply_then_gives_up():
    cand = find_candidates([run(i, "pip") for i in range(3)], 3)[0]
    rule, log = Compiler(scripted("not json", GOOD)).draft(cand)
    assert rule is not None and len(log["errors"]) == 1
    bad_op = json.dumps({"scope": [{"op": "rm_rf", "glob": "*"}]})
    rule, log = Compiler(scripted(bad_op, bad_op)).draft(cand)
    assert rule is None and len(log["errors"]) == 2


def test_invalid_action_template_is_rejected():
    cand = find_candidates([run(i, "pip") for i in range(3)], 3)[0]
    cand.shape = {"type": "pip_install", "package": "not valid!!"}
    rule, log = Compiler(scripted(GOOD, GOOD), validate_action=validate_fix).draft(cand)
    assert rule is None and "action template invalid" in log["errors"][0]


def test_repeat_streak_with_same_fix_is_not_a_new_rule():
    kinds = "imp imp imp pip imp imp imp pip pip pip".split()      # imp-streak, pip, imp-streak again, pip-streak
    cands = find_candidates([run(i, k) for i, k in enumerate(kinds, 1)], 3)
    assert len(cands) == 3
    assert [c.shape["type"] for c in dedupe_candidates(cands)] == ["fix_import", "pip_install"]