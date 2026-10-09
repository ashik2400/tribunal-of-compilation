"""Draw the headline chart from runs/experiment_results.json.
    python -m scripts.plot_experiment [--seed-index 0]
Needs matplotlib (pip install matplotlib)."""
import argparse, json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ap = argparse.ArgumentParser()
ap.add_argument("--results", default="runs/experiment_results.json")
ap.add_argument("--out", default="runs/experiment_chart.png")
ap.add_argument("--seed-index", type=int, default=0, help="which run to draw in the two timeline panels")
args = ap.parse_args()

data = json.loads(Path(args.results).read_text())
cfg, runs = data["config"], data["runs"]
names = ["no_shortcuts", "naive", "audited"]
label = {"no_shortcuts": "No shortcuts", "naive": "Naive compile\n(no Auditor)", "audited": "Audited compile"}
color = {"no_shortcuts": "#8a8a8a", "naive": "#d1495b", "audited": "#2a9d8f"}
n_seeds = len(runs)


def stat(name, key):
    v = [r["conditions"][name]["metrics"][key] for r in runs]
    return sum(v) / len(v), min(v), max(v)


def bars(ax, key, title, ylabel):
    for x, n in enumerate(names):
        m, lo, hi = stat(n, key)
        ax.bar(x, m, color=color[n], width=0.6)
        ax.errorbar(x, m, yerr=[[m - lo], [hi - m]], color="black", capsize=4, lw=1)
        ax.text(x, hi, f" {m:.1f}", ha="center", va="bottom", fontsize=9)
    ax.set_xticks(range(3), [label[n] for n in names], fontsize=8)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel)
    ax.spines[["top", "right"]].set_visible(False)


fig, axs = plt.subplots(2, 2, figsize=(11, 8))
bars(axs[0, 0], "llm_free_pct", "Tasks handled with no LLM call", "% of stream")
bars(axs[0, 1], "wrong_rule_decisions", "Wrong fixes served silently by rules", "count per stream")

run = runs[args.seed_index]
steps, k = cfg["mean_steps"], cfg["k"]
n_tasks = run["conditions"]["naive"]["metrics"]["tasks"]
ax_w, ax_c = axs[1, 0], axs[1, 1]
for n in names:
    c = run["conditions"][n]
    wrong, cost, w, total = [], [], 0, 0.0
    per_task_shadow = {}
    for e in c["shadow_events"]:
        per_task_shadow[e["i"]] = per_task_shadow.get(e["i"], 0) + k * steps
    for row in c["timeline"]:
        w += (row["handled_by"] == "rule" and not row["correct"])
        total += (steps if row["handled_by"] == "solver" else 0) + per_task_shadow.get(row["i"], 0)
        wrong.append(w); cost.append(total)
    ax_w.step(range(n_tasks), wrong, where="post", color=color[n], label=label[n].replace("\n", " "), lw=2)
    ax_c.plot(range(n_tasks), cost, color=color[n], lw=2)
    for i, _ in c["demotions"]:
        ax_w.axvline(i, color=color[n], ls=":", lw=1.2)
        ax_w.annotate("rule demoted", (i + 0.8, max(max(wrong) * 0.5, 0.5)), fontsize=8, color=color[n], ha="left")
first_twin = next((r["i"] for r in run["conditions"]["naive"]["timeline"] if r["variant"] != run["conditions"]["naive"]["timeline"][0]["variant"]), None)
for ax in (ax_w, ax_c):
    if first_twin is not None:
        ax.axvline(first_twin, color="#555", ls="--", lw=1)
    ax.set_xlabel("task index in stream")
    ax.spines[["top", "right"]].set_visible(False)
if first_twin is not None:
    ax_w.annotate("first twin arrives", (first_twin, 0), xytext=(first_twin + 1, 0.3), fontsize=8, color="#555")
ax_w.set_title(f"Cumulative wrong rule fixes (stream {args.seed_index})", fontsize=10)
ax_w.legend(fontsize=8, frameon=False, loc="upper left")
ax_c.set_title("Cumulative LLM calls incl. shadow-checks (estimated)", fontsize=10)
ax_c.set_ylabel("calls")
fig.suptitle(f"No shortcuts vs naive vs audited compile   |   {n_seeds} stream(s); bars = mean, whiskers = min..max\n"
             "Solver and shadow oracle replayed as correct; LLM calls estimated from measured steps per task",
             fontsize=9)
fig.tight_layout(rect=(0, 0, 1, 0.93))
Path(args.out).parent.mkdir(parents=True, exist_ok=True)
fig.savefig(args.out, dpi=150)
print("saved", args.out)
