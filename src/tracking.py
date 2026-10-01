"""MLflow tracking: every audited draft is one run (the 'training run' of the MLOps mapping).
mlflow is optional: pass a module/stub explicitly, or it is imported lazily and skipped if missing."""
from __future__ import annotations

import json


def log_audit(report, mlflow=None, experiment: str = "tribunal-compilation") -> bool:
    if mlflow is None:
        try:
            import mlflow  # type: ignore
        except ImportError:
            return False
    twins = [t for r in report.rounds for t in r.twins]
    count = lambda v: sum(t.verdict == v for t in twins)
    mlflow.set_experiment(experiment)
    with mlflow.start_run(run_name=report.rule_id):
        mlflow.log_params({"rule_id": report.rule_id, "signature": report.signature[:250],
                           "action": json.dumps(report.initial_rule["action"])[:250],
                           "initial_scope": json.dumps(report.initial_rule["scope"])[:250],
                           "final_verdict": report.final_verdict})
        mlflow.log_metrics({"repairs_used": report.repairs_used, "rounds": len(report.rounds),
                            "twins_pass": count("PASS"), "twins_break": count("BREAK"),
                            "twins_inconclusive": count("INCONCLUSIVE"), "twins_invalid": count("INVALID"),
                            "promoted": float(report.final_verdict == "passed")})
        mlflow.log_dict(report.to_dict(), "audit_report.json")
    return True
