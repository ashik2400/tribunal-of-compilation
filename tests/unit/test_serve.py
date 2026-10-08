import pytest
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from src.serve import create_app
from tests.unit.test_auditor import LOCAL, MISSING
from tests.unit.test_router import registry


def client(tmp_path, solver=None):
    if not (tmp_path / "reg.json").exists():
        registry(tmp_path / "reg.json")
    def factory():
        if solver is None:
            raise RuntimeError("no GROQ key")
        return solver
    return TestClient(create_app(str(tmp_path / "reg.json"), factory, log_path=str(tmp_path / "log.jsonl")))


def test_health_and_rules(tmp_path):
    c = client(tmp_path)
    assert c.get("/health").json() == {"status": "ok", "production_rules": 2}
    assert {r["state"] for r in c.get("/rules").json()} == {"production"}


def test_solve_uses_a_rule_with_no_llm(tmp_path):
    c = client(tmp_path)
    r = c.post("/solve", json={"files": LOCAL, "entry_point": "main.py"})
    assert r.status_code == 200
    body = r.json()
    assert body["handled_by"] == "rule" and body["rule_id"] == "B" and body["llm_calls"] == 0
    assert body["fix"]["type"] == "fix_import"


def test_unmatched_task_needs_the_solver_and_reports_503_when_unavailable(tmp_path):
    c = client(tmp_path)
    r = c.post("/solve", json={"files": {**MISSING, "vendor/colorutils/__init__.py": "x = 1\n"}, "entry_point": "main.py"})
    assert r.status_code == 503 and "Solver is unavailable" in r.json()["detail"]
    ok = client(tmp_path, solver=lambda t: ({"type": "pip_install", "package": "colorutils"}, True, 4))
    r = ok.post("/solve", json={"files": {**MISSING, "vendor/colorutils/__init__.py": "x = 1\n"}, "entry_point": "main.py"})
    assert r.status_code == 200 and r.json()["handled_by"] == "solver"


def test_unsafe_or_invalid_requests_are_refused(tmp_path):
    c = client(tmp_path)
    assert c.post("/solve", json={"files": {"main.py": "import subprocess\n"}, "entry_point": "main.py"}).status_code == 400
    assert c.post("/solve", json={"files": MISSING, "entry_point": "nope.py"}).status_code == 400
    assert c.post("/solve", json={"files": {"main.py": "print(1)\n"}, "entry_point": "main.py"}).status_code == 422   # does not fail


def test_a_demotion_is_visible_to_the_running_service(tmp_path):
    c = client(tmp_path, solver=lambda t: ({"type": "pip_install", "package": "colorutils"}, True, 4))
    assert c.post("/solve", json={"files": MISSING, "entry_point": "main.py"}).json()["handled_by"] == "rule"
    from src.registry import Registry
    Registry(tmp_path / "reg.json").demote("A", "drift")                # no restart
    assert c.post("/solve", json={"files": MISSING, "entry_point": "main.py"}).json()["handled_by"] == "solver"
