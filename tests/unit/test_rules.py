from src.rules import Predicate, Rule, fill, signature_of

TB = "Traceback (most recent call last):\n  File 'main.py', line 1\nModuleNotFoundError: No module named 'colorutils'"


def test_signature_ignores_the_specific_module():
    a = signature_of(TB)
    b = signature_of(TB.replace("colorutils", "requests"))
    assert a == ("ModuleNotFoundError: No module named '{}'", ["colorutils"])
    assert a[0] == b[0] and b[1] == ["requests"]


def test_fill_only_touches_numbered_placeholders():
    assert fill("import {0}; d = {'a': 1}", ["x"]) == "import x; d = {'a': 1}"
    assert fill("{3}", ["x"]) == "{3}"


def test_predicates_and_root_glob():
    files = {"main.py": "import colorutils\n", "utils/colorutils.py": "def f(): pass\n"}
    assert Predicate(op="file_exists", glob="**/{0}.py").holds(files, ["colorutils"])
    assert Predicate(op="file_absent", glob="**/{0}.py").holds({"main.py": ""}, ["colorutils"])
    assert Predicate(op="file_absent", glob="**/{0}.py").holds({"main.py": ""}, ["colorutils"])
    assert Predicate(op="file_exists", glob="**/main.py").holds(files, [])       # root file matched
    assert Predicate(op="text_contains", glob="main.py", text="import {0}").holds(files, ["colorutils"])
    assert Predicate(op="text_absent", glob="main.py", text="zzz").holds(files, [])


def test_rule_matches_and_fills_action():
    r = Rule(rule_id="r", signature="sig", action={"type": "pip_install", "package": "{0}"},
             scope=[Predicate(op="file_absent", glob="**/{0}.py")])
    assert r.matches("sig", ["requests"], {"main.py": ""})
    assert not r.matches("sig", ["requests"], {"utils/requests.py": ""})
    assert not r.matches("other", ["requests"], {"main.py": ""})
    assert r.fix(["requests"]) == {"type": "pip_install", "package": "requests"}
