"""HTTP service around the Router.   uvicorn src.serve:app --port 8000
LOCAL USE ONLY: submitted repos are executed (after a static screen) to verify fixes. Do not expose this
publicly until it runs inside the container with proper isolation.
The registry file is re-read on EVERY request, so a demotion made with registry_cli takes effect at once."""
from __future__ import annotations

import os
import uuid
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from src.domain.repo_repair.audit_backend import build_task, decision_key, screen_files
from src.domain.repo_repair.schema import GroundTruthFix, RepoRepairTask
from src.domain.repo_repair.solver_factory import default_solver_factory
from src.domain.repo_repair.solver_tools import verify_fix_explained
from src.registry import Registry
from src.router import Router


class SolveRequest(BaseModel):
    files: dict[str, str]
    entry_point: str
    traceback: str | None = None          # optional; if omitted the entry point is run to obtain it


def create_app(registry_path: str | None = None, solver_factory=default_solver_factory,
               log_path: str | None = "runs/router_log.jsonl") -> FastAPI:
    registry_path = registry_path or os.getenv("REGISTRY_PATH", "rules/registry.json")
    app = FastAPI(title="Tribunal of Compilation router")
    state: dict = {"solver": None}

    def registry() -> Registry:
        return Registry(registry_path, same_decision=lambda a, b: decision_key(a) == decision_key(b))

    def solver(task):
        if state["solver"] is None:
            try:
                state["solver"] = solver_factory()
            except Exception as e:
                raise HTTPException(503, f"no rule matched and the Solver is unavailable: {e}")
        return state["solver"](task)

    @app.get("/health")
    def health():
        return {"status": "ok", "production_rules": len(registry().production_rules())}

    @app.get("/rules")
    def rules():
        return [{"rule_id": rid, "state": e.state, "version": e.version, "action": e.rule.action,
                 "scope": [p.model_dump(exclude_none=True) for p in e.rule.scope]}
                for rid, e in registry().entries.items()]

    @app.post("/solve")
    def solve(req: SolveRequest):
        reason = screen_files(req.files)
        if reason:
            raise HTTPException(400, f"refused: {reason}")
        if req.entry_point not in req.files:
            raise HTTPException(400, "entry_point must be one of the files")
        task_id = f"req_{uuid.uuid4().hex[:8]}"
        try:
            if req.traceback is None:
                task = build_task(req.files, req.entry_point, task_id)
            else:
                task = RepoRepairTask(task_id=task_id, bug_family="request", variant="request", files=req.files,
                                      entry_point=req.entry_point, traceback=req.traceback,
                                      ground_truth=GroundTruthFix(description="n/a", action={"type": "unknown"}))
        except ValueError as e:
            raise HTTPException(422, str(e))
        router = Router(registry(), solver, verify=verify_fix_explained, log_path=log_path)
        return router.route(task).__dict__

    return app


app = create_app()
