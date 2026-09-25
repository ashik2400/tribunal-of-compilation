# Tribunal of Compilation: Adversarial Roles for Reliable Agent Automation

A multi-agent system that compiles agent-solved task patterns into cheap
deterministic shortcuts — while a second, adversarial agent actively hunts
for shortcuts that *look* safe but would silently misfire on close-but-
different future cases, before they're ever trusted.

Built end-to-end as an MLOps pipeline: every compile decision is a tracked
"training run," every promoted rule is a registered "model," and drift
monitoring is the mechanism that catches silent failure.

## The gap this fills

Existing 2026 systems that compile agent-solved patterns into deterministic
scripts measure cost reduction and post-compilation accuracy. None isolate
**silent misclassification**: a rule can look validated after a few clean
successes and still be wrong on a near-duplicate case the compiler didn't
distinguish — and since it now runs with zero LLM oversight, nothing flags
the wrong answer. This project makes that **false-compilation rate** the
central, measured metric.

## Architecture

Four roles, opposed incentives:

- **Solver** — agentic ReAct/Reflexion loop, solves incoming tasks, returns
  solution + confidence.
- **Compiler** — after N consistent high-confidence solves of a pattern,
  drafts a deterministic rule + a claimed valid input scope.
- **Auditor** — adversarially generates near-duplicate "twin" inputs
  designed to fall inside the claimed scope but require a different answer;
  tests the draft rule against them before it's trusted.
- **Router** — matches incoming tasks against the *promoted* rule library;
  executes the rule (no LLM call) on a match, else defers to Solver.

## Status

- [x] Repo scaffold + version control (this commit)
- [ ] Task pipeline & data versioning
- [ ] Solver
- [ ] Compile + Audit tracking
- [ ] Rule registry
- [ ] Router + packaging
- [ ] CI/CD/CT
- [ ] Shadow-check + monitoring loop
- [ ] Integration + 3-way eval
- [ ] Stretch: MLOps-domain twin cases
- [ ] Buffer: writeup, demo rehearsal

## Project structure

```
tribunal-of-compilation/
├── data/
│   ├── raw/            # generator configs, seed sabotaged-repo templates
│   └── processed/      # generated task sets (DVC-tracked)
├── src/
│   └── domain/         # pluggable domain adapter (repo-repair)
├── rules/              # promoted rule artifacts (the "model" equivalent)
├── tests/
│   ├── unit/
│   └── regression/     # accumulated twin-case suite, replayed every CI run
├── configs/
│   └── config.yaml     # single source of truth for paths/thresholds/models
├── requirements.txt
├── .gitignore
└── README.md
```

## Setup

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
dvc init
```
