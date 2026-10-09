"""Milestone 9: no-shortcuts vs naive-compile vs audited-compile on the same stream. Spends ZERO LLM tokens.
    python -m scripts.run_experiment [--seeds 5]
Needs: runs/solver_runs.jsonl (measured cost/accuracy), runs/rule_drafts.jsonl (unaudited drafts),
       rules/registry.json (audited rules in production)."""
import argparse, json, statistics
from pathlib import Path

from src.domain.repo_repair.audit_backend import decision_key, make_backend
from src.domain.repo_repair.generator import RepoRepairDomain
from src.domain.repo_repair.solver_tools import is_correct, verify_fix_explained
from src.experiment import build_stream, run_condition, shape_key
from src.registry import Registry
from src.rules import Rule

ap = argparse.ArgumentParser()
ap.add_argument("--seeds", type=int, default=5, help="repeat with different streams/samples (one run is an anecdote)")
ap.add_argument("--n-first", type=int, default=20)
ap.add_argument("--n-mixed-first", type=int, default=20)
ap.add_argument("--n-mixed-twin", type=int, default=20)
ap.add_argument("--shadow-rate", type=float, default=0.2)
ap.add_argument("--shadow-every", type=int, default=5)
ap.add_argument("--k", type=int, default=3)
ap.add_argument("--demote-after", type=int, default=1)
ap.add_argument("--solver-log", default="runs/solver_runs.jsonl")
ap.add_argument("--drafts", default="runs/rule_drafts.jsonl")
ap.add_argument("--registry", default="rules/registry.json")
ap.add_argument("--out", default="runs/experiment_results.json")
args = ap.parse_args()

solver_runs = [json.loads(l) for l in Path(args.solver_log).read_text().splitlines() if l.strip()]
mean_steps = statistics.mean(r["steps"] for r in solver_runs)
solver_acc = sum(r["correct"] for r in solver_runs) / len(solver_runs)

naive_lib = {}                                   # first (unaudited) draft per rule
for line in Path(args.drafts).read_text().splitlines():
    d = json.loads(line)
    if d["rule"]:
        rule = Rule.model_validate(d["rule"])
        if rule.rule_id not in {r.rule_id for r in naive_lib.values()}:
            naive_lib[shape_key(rule.action)] = rule
reg = Registry(args.registry, same_decision=lambda a, b: decision_key(a) == decision_key(b))
audited_lib = {shape_key(e.rule.action): e.rule for e in reg.entries.values() if e.state == "production"}
if set(naive_lib) != set(audited_lib):
    raise SystemExit(f"drafts and production rules cover different fix shapes:\n {sorted(naive_lib)}\n {sorted(audited_lib)}")
print(f"measured from {len(solver_runs)} real Solver runs: {mean_steps:.2f} LLM calls/task, {solver_acc:.0%} accurate")
print(f"naive library:   {[ (r.rule_id, [p.op for p in r.scope]) for r in naive_lib.values()]}")
print(f"audited library: {[ (r.rule_id, [p.op for p in r.scope]) for r in audited_lib.values()]}\n")

domain = RepoRepairDomain()
conditions = {"no_shortcuts": ({}, False), "naive": (naive_lib, True), "audited": (audited_lib, False)}
runs_out = []
for seed in range(args.seeds):
    tasks = build_stream(domain.generate_task, args.n_first, args.n_mixed_first, args.n_mixed_twin, seed=seed)
    row = {"seed": seed, "conditions": {}}
    for name, (lib, naive) in conditions.items():
        r = run_condition(name, tasks, lib, naive=naive, is_correct=is_correct, verify=verify_fix_explained,
                          make_backend_fn=lambda fn: make_backend(solve_fn=fn), mean_steps=mean_steps,
                          shadow_rate=args.shadow_rate, shadow_every=args.shadow_every, k=args.k, seed=seed,
                          demote_after=args.demote_after)
        row["conditions"][name] = {"metrics": r.metrics, "timeline": r.timeline, "installs": r.installs,
                                   "demotions": r.demotions, "blocked": r.blocked, "shadow_events": r.shadow_events}
    runs_out.append(row)
    print(f"seed {seed}: " + "  ".join(
        f"{n}: free={c['metrics']['llm_free_pct']}% wrong={c['metrics']['wrong_rule_decisions']} "
        f"calls={c['metrics']['llm_calls']}" for n, c in row["conditions"].items()))

def agg(name, key):
    vals = [r["conditions"][name]["metrics"][key] for r in runs_out]
    vals = [v for v in vals if v is not None]
    return f"{statistics.mean(vals):.1f} [{min(vals)}..{max(vals)}]" if vals else "-"

print(f"\n{args.seeds} seed(s), stream = {args.n_first} + {args.n_mixed_first + args.n_mixed_twin} tasks, "
      f"shadow rate {args.shadow_rate} every {args.shadow_every} tasks  (mean [min..max])")
print(f"{'':14}{'LLM-free %':>18}{'wrong rule fixes':>20}{'LLM calls (est.)':>22}{'detection latency':>22}{'undetected':>12}")
for name in conditions:
    und = sum(r["conditions"][name]["metrics"]["undetected"] for r in runs_out)
    print(f"{name:14}{agg(name, 'llm_free_pct'):>18}{agg(name, 'wrong_rule_decisions'):>20}"
          f"{agg(name, 'llm_calls'):>22}{agg(name, 'detection_latency_tasks'):>22}{und:>8}/{args.seeds}")

Path(args.out).parent.mkdir(parents=True, exist_ok=True)
Path(args.out).write_text(json.dumps({"config": vars(args) | {"mean_steps": mean_steps, "solver_accuracy": solver_acc},
                                      "runs": runs_out}))
print(f"\nsaved {args.out}   (Solver and shadow oracle are REPLAYED as correct; LLM calls are estimates)")
