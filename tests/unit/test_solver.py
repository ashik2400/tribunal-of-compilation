import json
from src.solver import Solver, Tool


def scripted(*replies):
    """Fake LLM that returns canned replies in order -> deterministic, zero API cost."""
    it = iter(replies)
    return lambda system, messages, **kw: next(it)


def act(name, **args): return json.dumps({"thought": "t", "action": name, "args": args})
def final(sol, c=0.9): return json.dumps({"thought": "t", "final": {"solution": sol, "confidence": c}})


TOOLS = {"read": Tool("read", "read(path)", lambda path: f"contents of {path}"),
         "boom": Tool("boom", "always fails", lambda: (_ for _ in ()).throw(RuntimeError("nope")))}


def test_solves_first_attempt_with_tool_use():
    s = Solver(scripted(act("read", path="a.py"), final("pip install x")), TOOLS, lambda t, sol: True)
    r = s.solve("task")
    assert r.verified and r.attempts == 1 and r.steps == 2 and r.confidence == 0.95


def test_reflexion_retries_after_failed_verification():
    llm = scripted(final("wrong"), "Lesson: check local imports first.", final("right"))
    s = Solver(llm, TOOLS, lambda t, sol: sol == "right")
    r = s.solve("task")
    assert r.verified and r.attempts == 2 and len(r.reflections) == 1
    assert r.confidence == 0.8   # 0.95 - 0.15


def test_recovers_from_tool_error_and_malformed_json():
    llm = scripted(act("boom"), "not json at all", final("ok"))
    r = Solver(llm, TOOLS, lambda t, sol: True).solve("task")
    assert r.verified and r.steps == 3
    assert "ERROR" in r.trace[0]["observation"] and "malformed" in r.trace[1]["observation"]


def test_gives_up_unverified_with_low_confidence():
    llm = scripted(final("bad1", 0.99), "r1", final("bad2", 0.99), "r2", final("bad3", 0.99))
    r = Solver(llm, TOOLS, lambda t, sol: False, max_attempts=3).solve("task")
    assert not r.verified and r.confidence <= 0.3 and r.self_reported_confidence == 0.99


def test_lenient_action_final_is_accepted():
    llm = scripted(json.dumps({"thought": "t", "action": "final",
                               "args": {"solution": "fix", "confidence": 0.9}}))
    r = Solver(llm, TOOLS, lambda t, sol: True).solve("task")
    assert r.verified and r.solution == "fix" and r.steps == 1


def test_invalid_format_is_bounced_without_burning_an_attempt():
    validate = lambda sol: None if isinstance(sol, dict) else "must be an object"
    llm = scripted(final("pip install x"), final({"type": "pip_install", "package": "x"}))
    r = Solver(llm, TOOLS, lambda t, sol: True, validate=validate).solve("task")
    assert r.verified and r.attempts == 1 and r.reflections == []
    assert "must be an object" in r.trace[0]["observation"]


def test_reflection_sees_the_real_rejection_reason():
    seen = []
    replies = iter([final("bad"), "lesson", final("good")])
    def llm(system, messages, **kw):
        seen.append(messages[-1]["content"])
        return next(replies)
    verify = lambda t, sol: (sol == "good", "the entry point still fails: NameError")
    Solver(llm, TOOLS, verify).solve("task")
    assert "NameError" in seen[1]      # the reflection prompt carries the verifier's reason