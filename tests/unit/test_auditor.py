import json
import pytest
from src.auditor import Auditor, RegressionSuite
from src.compiler import Candidate, Compiler
from src.domain.repo_repair.audit_backend import make_backend, screen_files
from src.rules import Predicate, Rule, signature_of

SIG = "ModuleNotFoundError: No module named '{}'"
PIP = {"type": "pip_install", "package": "{0}"}
IMP = {"type": "fix_import", "replace_line": "import {0}", "with_line": "from utils import {0}"}
IMP_C = {"type": "fix_import", "replace_line": "import colorutils", "with_line": "from utils import colorutils"}
PIP_C = {"type": "pip_install", "package": "colorutils"}
MISSING = {"main.py": "import colorutils\nprint(colorutils.hex_to_rgb('#fff'))\n"}
LOCAL = {"main.py": "import colorutils\nprint(colorutils.hex_to_rgb('#fff'))\n",
         "utils/colorutils.py": "def hex_to_rgb(h):\n    return (255, 255, 255)\n"}


def ex(task_id, files, fix):
    return {"task_id": task_id, "captures": ["colorutils"], "files": files, "fix": fix}


def rule_a():   # the real bad rule from your run: never checks for a local module
    return Rule(rule_id="A", signature=SIG, action=PIP,
                scope=[Predicate(op="text_contains", glob="**/*.py", text="import {0}"),
                       Predicate(op="file_absent", glob="requirements.txt")])


def rule_b():   # the real good rule
    return Rule(rule_id="B", signature=SIG, action=IMP,
                scope=[Predicate(op="file_exists", glob="utils/{0}.py"),
                       Predicate(op="text_contains", glob="**/*.py", text="import {0}")])


CAND_A = Candidate(SIG, PIP, [ex("m1", MISSING, PIP_C), ex("m2", {"main.py": "import colorutils\nx = 1\n"}, PIP_C)])
CAND_B = Candidate(SIG, IMP, [ex("l1", LOCAL, IMP_C)])


def oracle(task):
    """Scripted Solver: the right answer is a local import iff utils/<module>.py exists."""
    _, caps = signature_of(task.traceback)
    if f"utils/{caps[0]}.py" in task.files:
        return {"type": "fix_import", "replace_line": f"import {caps[0]}",
                "with_line": f"from utils import {caps[0]}"}, True
    return {"type": "pip_install", "package": caps[0]}, True


def scripted(*replies):
    it = iter(replies)
    return lambda system, messages, **kw: next(it)


def twin(files, expected, entry="main.py", why="t"):
    return {"files": files, "entry_point": entry, "expected_fix": expected, "why": why}


def reply(*twins):
    return json.dumps({"twins": list(twins)})


def local_variants():
    return [twin({"main.py": "import colorutils\nprint(colorutils.hex_to_rgb('#0f0'))\n",
                  "utils/colorutils.py": "def hex_to_rgb(h):\n    return (0, 255, 0)\n"}, IMP_C),
            twin({"main.py": "import colorutils\nprint(colorutils.hex_to_rgb('#00f'))\nprint('done')\n",
                  "utils/colorutils.py": "def hex_to_rgb(h):\n    return (0, 0, 255)\n",
                  "utils/__init__.py": ""}, IMP_C),
            twin({"app.py": "import colorutils\nprint(colorutils.hex_to_rgb('#f00'))\n",
                  "utils/colorutils.py": "def hex_to_rgb(h):\n    return (255, 0, 0)\n"}, IMP_C, entry="app.py")]


def missing_variants():
    return [twin({"main.py": f"import colorutils\nprint({i}, colorutils.hex_to_rgb('#fff'))\n"}, PIP_C)
            for i in range(3)]


def auditor(llm, solve=oracle, **kw):
    kw = {"k": 3, "n_twins": 3, "min_pass": 3, "gen_rounds": 2, **kw}
    return Auditor(llm, make_backend(solve_fn=solve), **kw)


# ---------- the headline behaviours ----------
def test_bad_rule_is_broken_by_the_local_module_twin():
    a = auditor(scripted(reply(local_variants()[0])))
    rd = a.audit_round(rule_a(), CAND_A)
    assert rd.verdict == "broken" and rd.breaks[0]["correct_fix"] == IMP_C
    assert rd.twins[0].verdict == "BREAK" and "unanimous" in rd.twins[0].reason


def test_good_rule_passes_with_enough_conclusive_twins():
    rd = auditor(scripted(reply(*local_variants()))).audit_round(rule_b(), CAND_B)
    assert rd.verdict == "passed" and sum(t.verdict == "PASS" for t in rd.twins) == 3


# ---------- gates: bad twins never count ----------
def test_invalid_twins_are_ignored_not_counted():
    bad = [twin(MISSING, PIP_C),                                            # outside rule B's scope
           twin({"main.py": "print(undefined_name)\n"}, IMP_C),             # different error
           twin({"main.py": "print(1)\n"}, IMP_C),                          # does not fail
           twin({"main.py": "import os\nimport colorutils\n", "utils/colorutils.py": "x=1\n"}, IMP_C),  # blocked
           twin(LOCAL, {"type": "rm_rf"}),                                  # invalid expected fix
           twin(LOCAL, IMP_C),                                              # copy of the example repo
           twin({"main.py": "print(undefined_name)\n"}, IMP_C)]             # repeat of an earlier proposal
    rd = auditor(scripted(reply(*bad), reply())).audit_round(rule_b(), CAND_B)
    assert [t.verdict for t in rd.twins].count("INVALID") == len(rd.twins) and rd.verdict == "inconclusive"
    reasons = " | ".join(t.reason for t in rd.twins)
    for expect in ("outside the rule's scope", "signature differs", "ran successfully", "blocked module",
                   "expected_fix invalid", "identical to a given example", "duplicate of an earlier"):
        assert expect in reasons


def test_expected_fix_that_does_not_work_is_invalid():
    wrong = {"type": "fix_import", "replace_line": "import colorutils", "with_line": "from nowhere import colorutils"}
    rd = auditor(scripted(reply(twin(LOCAL_2 := {**LOCAL, "extra.py": "y = 2\n"}, wrong)), reply())).audit_round(rule_b(), CAND_B)
    assert rd.twins[0].verdict == "INVALID" and "does not actually fix" in rd.twins[0].reason


# ---------- fail closed ----------
def test_solver_disagreement_is_inconclusive_and_not_promoted():
    flip = iter([0, 1] * 20)
    def flaky(task):
        fix, ok = oracle(task)
        return (fix, ok) if next(flip) == 0 else ({"type": "pip_install", "package": "colorutils"}, True)
    rd = auditor(scripted(reply(*local_variants()), reply()), solve=flaky).audit_round(rule_b(), CAND_B)
    assert rd.verdict == "inconclusive" and not rd.breaks


def test_disputed_twin_does_not_condemn_the_rule():
    # Three different answers: rule says "from utils import x", Auditor claims "import utils.x as x",
    # Solver says pip_install. Nobody has two votes for a different fix -> inconclusive, NOT a break.
    always_pip = lambda task: ({"type": "pip_install", "package": "colorutils"}, True)
    other_import = {"type": "fix_import", "replace_line": "import colorutils",
                    "with_line": "import utils.colorutils as colorutils"}
    be = make_backend(solve_fn=always_pip)
    be.decision_key = lambda f: json.dumps(f, sort_keys=True)          # treat different imports as different decisions
    aud = Auditor(scripted(reply(twin({**LOCAL, "z.py": "1\n"}, other_import)), reply()), be,
                  k=3, n_twins=3, min_pass=3, gen_rounds=2)
    rd = aud.audit_round(rule_b(), CAND_B)
    assert rd.twins[0].verdict == "INCONCLUSIVE" and "disputed" in rd.twins[0].reason
    assert rd.verdict == "inconclusive" and not rd.breaks


def test_unverified_solver_is_inconclusive():
    rd = auditor(scripted(reply(*local_variants()), reply()),
                 solve=lambda t: (None, False)).audit_round(rule_b(), CAND_B)
    assert rd.verdict == "inconclusive"


def test_rules_that_fail_to_run_break_without_asking_the_solver():
    def no_solver(task):
        raise AssertionError("Solver must not be consulted")
    r = Rule(rule_id="X", signature=SIG, scope=[Predicate(op="file_exists", glob="utils/{0}.py")],
             action={"type": "fix_import", "replace_line": "import {0}", "with_line": "from elsewhere import {0}"})
    cand = Candidate(SIG, r.action, [ex("l1", LOCAL, IMP_C)])
    rd = auditor(scripted(reply(local_variants()[0])), solve=no_solver).audit_round(r, cand)
    assert rd.verdict == "broken" and "fails to run" in rd.twins[0].reason


def test_vacuous_rule_is_rejected():
    r = Rule(rule_id="V", signature=SIG, action=PIP, scope=[Predicate(op="file_exists", glob="nothing.py")])
    rd = auditor(scripted()).audit_round(r, CAND_A)
    assert rd.verdict == "vacuous"


# ---------- regression suite ----------
def test_stored_breaking_twin_is_replayed_without_any_llm(tmp_path):
    suite = RegressionSuite(tmp_path / "twins.jsonl")
    auditor(scripted(reply(local_variants()[0]))).audit_round(rule_a(), CAND_A, suite)
    assert len(suite.records) == 1
    suite2 = RegressionSuite(tmp_path / "twins.jsonl")                    # reloaded from disk
    rd = auditor(scripted()).audit_round(rule_a(), CAND_A, suite2)          # scripted() has NO replies
    assert rd.verdict == "broken" and rd.regression_failures == 1


# ---------- repair loop ----------
FIXED_SCOPE = json.dumps({"scope": [{"op": "file_absent", "glob": "**/{0}.py"},
                                    {"op": "text_contains", "glob": "**/*.py", "text": "import {0}"}],
                          "rationale": "no local module of that name"})


def test_repair_loop_fixes_the_bad_rule_and_reaudits_it():
    aud = auditor(scripted(reply(local_variants()[0]), reply(*missing_variants())))
    rep = aud.run(rule_a(), CAND_A, Compiler(scripted(FIXED_SCOPE)))
    assert rep.final_verdict == "passed" and rep.repairs_used == 1
    assert [r.verdict for r in rep.rounds] == ["broken", "passed"]
    assert any(p["op"] == "file_absent" for p in rep.final_rule["scope"])


def test_vacuous_repair_is_rejected_and_rule_ends_deferred():
    cheat = json.dumps({"scope": [{"op": "file_exists", "glob": "nothing.py"}], "rationale": "never fires"})
    aud = auditor(scripted(reply(local_variants()[0])))
    rep = aud.run(rule_a(), CAND_A, Compiler(scripted(cheat, cheat)))
    assert rep.final_verdict == "deferred" and rep.repairs_used == 2
    assert "no longer covers the original examples" in rep.reason


def test_unrepairable_rule_is_deferred_after_two_attempts():
    same = json.dumps({"scope": [{"op": "file_absent", "glob": "requirements.txt"}], "rationale": "same"})
    aud = auditor(scripted(reply(local_variants()[0]), reply(local_variants()[1]), reply(local_variants()[2])))
    rep = aud.run(rule_a(), CAND_A, Compiler(scripted(same, same)))
    assert rep.final_verdict == "deferred" and rep.repairs_used == 2


# ---------- safety screen + tracking ----------
def test_screen_blocks_dangerous_or_oversized_repos():
    assert screen_files({"main.py": "import subprocess\n"})
    assert screen_files({"main.py": "open('x').read()\n"})
    assert screen_files({"../evil.py": "x=1\n"}) and screen_files({"/abs.py": "x=1\n"})
    assert screen_files({"main.py": "x = 1\n" * 400})
    assert screen_files({"main.py": "import colorutils\n", "utils/colorutils.py": "def f(): pass\n"}) is None


def test_tracking_logs_one_run_per_rule():
    from src.tracking import log_audit

    class Stub:
        def __init__(self): self.calls = []
        def set_experiment(self, n): self.calls.append(("exp", n))
        def start_run(self, run_name): self.calls.append(("run", run_name)); return self
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def log_params(self, p): self.calls.append(("params", p))
        def log_metrics(self, m): self.calls.append(("metrics", m))
        def log_dict(self, d, name): self.calls.append(("dict", name))

    rep = auditor(scripted(reply(*local_variants()))).run(rule_b(), CAND_B, Compiler(scripted()))
    stub = Stub()
    assert log_audit(rep, mlflow=stub)
    metrics = dict(c for c in stub.calls if c[0] == "metrics")["metrics"]
    assert metrics["promoted"] == 1.0 and metrics["twins_pass"] == 3


def test_rejections_are_fed_back_and_raw_replies_kept():
    prompts = []
    replies = iter([reply(twin(LOCAL, IMP_C), twin({"main.py": "import colorutils\n"}, PIP_C)), reply()])
    def llm(system, messages, **kw):
        prompts.append(messages[-1]["content"])
        return next(replies)
    rd = auditor(llm).audit_round(rule_b(), CAND_B)
    assert len(rd.gen_replies) == 2 and rd.gen_replies[0].startswith('{"twins"')
    second = prompts[1]
    assert "identical to a given example" in second and "utils/colorutils.py" in second   # reason + file list
    assert "outside the rule's scope" in second


def test_notes_tell_the_generator_the_environment_facts():
    from src.domain.repo_repair.audit_backend import NOTES
    assert "MUST fail" in NOTES and "colorutils" in NOTES and "requests" in NOTES


def test_double_escaped_newlines_are_repaired_not_rejected():
    garbled = twin({"main.py": "import colorutils\\nprint(colorutils.hex_to_rgb('#abc'))\\n",
                    "utils/colorutils.py": "def hex_to_rgb(h):\\n    return (1, 2, 3)\\n"}, IMP_C)
    assert "\\n" in garbled["files"]["main.py"] and "\n" not in garbled["files"]["main.py"]   # really garbled
    rd = auditor(scripted(reply(garbled, *local_variants()[:2]))).audit_round(rule_b(), CAND_B)
    assert rd.twins[0].verdict == "PASS"


def test_solver_runs_are_recorded_for_inconclusive_twins():
    rd = auditor(scripted(reply(local_variants()[0]), reply()),
                 solve=lambda t: (None, False)).audit_round(rule_b(), CAND_B)
    t = rd.twins[0]
    assert t.verdict == "INCONCLUSIVE" and len(t.solver_runs) == 3 and t.solver_runs[0]["verified"] is False


def test_nonfailing_repo_message_explains_why():
    rd = auditor(scripted(reply(twin({"main.py": "print(1)\n"}, IMP_C)), reply())).audit_round(rule_b(), CAND_B)
    assert "exit 0" in rd.twins[0].reason