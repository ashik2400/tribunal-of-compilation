import json
from src.auditor import Auditor, RegressionSuite
from src.compiler import Compiler
from src.domain.repo_repair.audit_backend import make_backend, mutate_repo
from src.domain.repo_repair.schema import GroundTruthFix, RepoRepairTask
from src.domain.repo_repair.solver_tools import is_correct
from src.evaluation import evaluate_rule
from tests.unit.test_auditor import (CAND_A, CAND_B, FIXED_SCOPE, IMP_C, MISSING, PIP_C, LOCAL, oracle, reply,
                                     missing_variants, rule_a, rule_b, scripted)

TB = "ModuleNotFoundError: No module named 'colorutils'"


def aud(llm, mutations=True, controls=False, **kw):
    kw = {"k": 3, "n_twins": 3, "min_pass": 3, "gen_rounds": 2, **kw}
    return Auditor(llm, make_backend(solve_fn=oracle, mutations=mutations, controls=controls), **kw)


# ---------- mutation operator ----------
def test_mutate_repo_adds_decoys_with_stubs_for_used_attributes():
    out = dict((tuple(sorted(f)), why) for f, why in mutate_repo(MISSING, ["colorutils"]))
    assert ("main.py", "utils/colorutils.py") in out and ("helpers/colorutils.py", "main.py") in out
    files = [f for f, _ in mutate_repo(MISSING, ["colorutils"]) if "utils/colorutils.py" in f][0]
    assert "def hex_to_rgb" in files["utils/colorutils.py"]            # stub provides what main.py calls


def test_mutate_repo_ignores_unsafe_or_missing_names():
    assert mutate_repo(MISSING, []) == [] and mutate_repo(MISSING, ["../evil"]) == []
    assert mutate_repo(LOCAL, ["colorutils"]) == []     # module already local: a decoy would be ambiguous
    assert mutate_repo({"main.py": "import colorutils\n", "lib/colorutils/__init__.py": "x = 1\n"}, ["colorutils"]) == []


# ---------- the headline: the false pass from your real run is now caught, with NO LLM call ----------
def test_bad_rule_is_broken_by_mutation_twin_without_any_llm_call():
    rd = aud(scripted()).audit_round(rule_a(), CAND_A)          # scripted() has no replies: LLM must not be called
    assert rd.verdict == "broken"
    t = [t for t in rd.twins if t.verdict == "BREAK"][0]
    assert t.source == "mutation" and "utils/colorutils.py" in t.files
    assert rd.breaks[0]["correct_fix"]["type"] == "fix_import"


def test_good_rule_with_local_module_examples_gets_no_ambiguous_mutations():
    from tests.unit.test_auditor import local_variants
    rd = aud(scripted(reply(*local_variants()))).audit_round(rule_b(), CAND_B)
    assert not any(t.source == "mutation" for t in rd.twins)          # nothing to mutate unambiguously
    assert rd.verdict == "passed"


def test_repair_after_mutation_break_then_pass_via_llm_twins():
    a = aud(scripted(reply(*missing_variants())))
    rep = a.run(rule_a(), CAND_A, Compiler(scripted(FIXED_SCOPE)))
    assert rep.final_verdict == "passed" and rep.repairs_used == 1
    assert [r.verdict for r in rep.rounds] == ["broken", "passed"]


def test_mutation_break_is_stored_and_replayed(tmp_path):
    suite = RegressionSuite(tmp_path / "t.jsonl")
    aud(scripted()).audit_round(rule_a(), CAND_A, suite)
    assert len(suite.records) == 1
    rd = Auditor(scripted(), make_backend(solve_fn=oracle), k=3).audit_round(rule_a(), CAND_A,
                                                                              RegressionSuite(tmp_path / "t.jsonl"))
    assert rd.verdict == "broken" and rd.regression_failures == 1


# ---------- ground-truth evaluation (independent of the Auditor) ----------
def task(variant, files, truth):
    return RepoRepairTask(task_id=variant, bug_family="import_error_twin", variant=variant, files=files,
                          entry_point="main.py", traceback=TB,
                          ground_truth=GroundTruthFix(description="d", action=truth))


TASKS = [task("missing_package", MISSING, PIP_C),
         task("needs_local_import", LOCAL, {"type": "fix_import", "replace_line": "import colorutils",
                                            "with_line": "from utils import colorutils"})]


def test_evaluation_measures_the_false_fire_the_auditor_missed():
    a = evaluate_rule(rule_a(), TASKS, is_correct)
    assert a["fires"] == 2 and a["false_fires"] == 1 and a["wrong_task_ids"] == ["needs_local_import"]
    b = evaluate_rule(rule_b(), TASKS, is_correct)
    assert b["fires"] == 1 and b["false_fires"] == 0 and b["coverage"] == 0.5


PARTIAL = '{"twins": [{"files": {"a"'


def test_groq_invalid_json_400_is_salvaged_not_fatal():
    from src.llm import salvage_failed_json

    class Bad(Exception):
        status_code = 400
        body = {"error": {"code": "json_validate_failed", "failed_generation": PARTIAL}}

    assert salvage_failed_json(Bad("json_validate_failed")) == PARTIAL
    class Other(Exception):
        status_code = 400
    assert salvage_failed_json(Other("something else")) is None
    assert salvage_failed_json(ValueError("x")) is None


# ---------- control twins ----------
def repaired_rule_a():
    from src.rules import Predicate, Rule
    from tests.unit.test_auditor import PIP, SIG
    return Rule(rule_id="A2", signature=SIG, action=PIP,
                scope=[Predicate(op="text_contains", glob="**/*.py", text="import {0}"),   # as drafted in your run
                       Predicate(op="file_absent", glob="**/{0}.py"),
                       Predicate(op="file_absent", glob="**/{0}/__init__.py")])


def test_controls_are_benign_and_safe_to_run():
    from src.domain.repo_repair.audit_backend import control_repo, screen_files
    twins = control_repo(MISSING, ["colorutils"])
    assert len(twins) == 4 and len({json.dumps(f, sort_keys=True) for f, _ in twins}) == 4
    for files, _ in twins:
        assert screen_files(files) is None and "import colorutils" in files["main.py"]
        assert not any(p.endswith("colorutils.py") for p in files)          # still missing: answer unchanged


def test_repaired_rule_is_confirmed_by_controls_without_any_llm_call():
    rd = aud(scripted(), mutations=True, controls=True).audit_round(repaired_rule_a(), CAND_A)
    assert rd.verdict == "passed" and not rd.gen_replies
    assert [t.verdict for t in rd.twins if t.source == "control"].count("PASS") >= 3
    assert all(t.verdict == "INVALID" for t in rd.twins if t.source == "mutation")   # decoys excluded by scope


def test_controls_alone_would_pass_the_bad_rule_so_decoys_must_run_first():
    # honest limit: benign controls cannot catch a bad rule...
    rd = aud(scripted(), mutations=False, controls=True).audit_round(rule_a(), CAND_A)
    assert rd.verdict == "passed"
    # ...which is why the adversarial decoys come first and stop the audit before controls are evaluated
    rd = aud(scripted(), mutations=True, controls=True).audit_round(rule_a(), CAND_A)
    assert rd.verdict == "broken" and not any(t.source == "control" for t in rd.twins)