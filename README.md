# Tribunal of Compilation: Adversarial Roles for Reliable Agent Automation

A multi-agent system that compiles agent-solved task patterns into cheap
deterministic shortcuts, while a second, adversarial agent hunts for shortcuts
that look safe but would silently misfire on close-but-different future cases,
before they are trusted.

Built as an MLOps pipeline: every compile decision is logged, every promoted rule
lives in a registry with a lifecycle, CI gates the registry, and shadow-checks
monitor production rules for silent failure.

## The problem

After a few consistent successes, a compiled rule can answer a task without an
LLM call. But a rule validated on a few clean successes can still be wrong on a
near-duplicate input that needs a different answer, and once the rule is in
production nothing checks it.

Example (repo repair): `ModuleNotFoundError: No module named 'colorutils'` is
usually fixed by `pip install colorutils`. If a local `utils/colorutils.py`
exists, the right fix is `from utils import colorutils`. Same traceback,
different fix. The project measures this failure directly:

    false-compilation rate = wrong decisions served by rules / decisions served by rules

## Architecture

| Role | Incentive | Notes |
| --- | --- | --- |
| Solver | Solve the task correctly | ReAct + Reflexion loop, repo-repair tools, executable verifier |
| Compiler | Ship a shortcut | Drafts a rule (signature, scope, action) after consistent solves |
| Auditor | Break the rule before it is trusted | Twins from regression replay, decoy mutations, controls and LLM proposals; the Solver is the oracle |
| Router | Apply promoted rules, defer the rest | Deterministic, no LLM |

Around them: a rule registry (promotion gates, scope-conflict check), a
regression suite of every twin that ever broke a rule, CI gates, and
shadow-checks that demote a rule when a re-run through the Solver diverges.

## Results

Replay of 5 streams of 60 repo-repair tasks (20 clean tasks, then a mix of
clean tasks and twins). Numbers are from `results/experiment_results.json`.

| Metric | No shortcuts | Naive compile (unaudited drafts) | Audited compile |
| --- | --- | --- | --- |
| Handled with no LLM call, mean (range) | 0% | 67.7% (53.3 to 95.0) | 90.0% (90.0 to 90.0) |
| Wrong fixes per stream, mean (range) | 0 | 7.6 (2 to 20) | 0 (0 to 0) |
| Streams with an undetected misfire | 0 of 5 | 1 of 5 | 0 of 5 |
| Estimated LLM calls per stream, mean | 282.0 | 175.8 | 152.3 |

Read these with the limitations below: the Solver and shadow-check oracle are
replayed from ground truth, LLM calls are estimated (4.7 calls per task,
measured over 20 real Solver runs), and the evaluation covers one domain and
one twin family. See `results/experiment_chart.png` for the plots.

## Status

| Milestone | Status | Git tag or commit |
| --- | --- | --- |
| 1. Repo scaffold, config discipline | Done | `9fffa82` |
| 2. Task pipeline, DVC, repo-repair adapter | Done | `1062151` |
| 3. Solver (ReAct + Reflexion) | Done | `milestone-3-solver` |
| 4. Compiler + Auditor, regression suite | Done | `milestone-4-audit` |
| 5. Rule registry | Done | shipped with `milestone-7-ci` |
| 6. Router, HTTP service, Docker | Done | `milestone-6-router` |
| 7. CI safety gate | Done | `milestone-7-ci` |
| 8. Shadow-check monitoring | Done | `milestone-8-shadow-check` |
| 9. Three-way comparison | Done | `milestone-9-experiment` |
| 10. MLOps-domain twins (stretch) | Not attempted | listed as future work |
| 11. Writeup, demo, polish | In progress | |

## Quickstart (Windows PowerShell)

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
copy .env.example .env      # add your LLM provider keys; .env is git-ignored

python -m pytest tests -q                 # 115 unit tests
python -m scripts.check_registry          # registry safety gate (also runs in CI)
python -m scripts.run_experiment --seeds 5 --out results\experiment_results.json
docker compose up --build                 # Router + rule library as an HTTP service
```

The experiment replays recorded Solver runs (`runs/solver_runs.jsonl`),
recorded rule drafts (`runs/rule_drafts.jsonl`) and the promoted rules in
`rules/registry.json`, so it needs no API calls. Routes of the HTTP service are
defined in `src/serve.py`.

## Repository layout

| Path | Contents |
| --- | --- |
| `src/solver.py`, `compiler.py`, `auditor.py`, `router.py` | The four roles |
| `src/registry.py`, `shadow.py`, `ci_checks.py`, `serve.py` | Registry, monitoring, CI gate, HTTP service |
| `src/experiment.py`, `scripts/` | Replay evaluation and its scripts |
| `src/domain/repo_repair/` | Domain adapter, task generator, audit backend |
| `rules/registry.json`, `tests/regression/twins.jsonl` | Production rules, stored regression twins |
| `audit_reports.jsonl` | Audit evidence: first-draft versus final rule scopes |
| `results/` | Experiment results and chart |

## Limitations

- Replay evaluation: Solver and shadow oracle are replayed from ground truth.
- One domain (repo repair) and one twin family (missing package versus local import).
- The audited rules were repaired against the same family the streams contain, so the evaluation is in-distribution.
- The recorded counterexamples came from the decoy-mutation operator and the regression suite; the LLM twin generator's own contribution is not isolated.
- Five streams and two rules: zero wrong fixes in 5 of 5 streams is evidence, not a guarantee.