# Investigation: stored regression twin from `rule_4876b859`

Status: **findings only.** No registry, regression or audit data was changed. Waiting on a decision.

## The record

First line of `tests/regression/twins.jsonl`:

| Field | Value |
| --- | --- |
| files | `main.py` (`import colorutils` / `print(colorutils.hex_to_rgb('#ffffff'))`), `utils/colorutils.py` (real `hex_to_rgb`), `lib/colorutils.py` (stub: `def hex_to_rgb(*args, **kwargs): return None`) |
| correct_fix | `{"type": "pip_install", "package": "colorutils"}` |
| why | "Solver unanimously (3 runs) chose a different fix than the rule (mutation twin)" |
| origin_rule | `rule_4876b859` (fix_import: `import {0}` -> `from utils import {0}`) |

In `audit_reports.jsonl`, round 0 of `rule_4876b859` is the source. That round has scope `file_exists utils/{0}.py` + `text_contains **/*.py 'import {0}'`. Its one BREAK is this repo: a `mutation` twin with solver_runs = 3 × `pip_install`, all verified. The Compiler's repair added `file_absent lib/{0}.py`, and round 1 passed with 3 LLM PASS.

## Reproduction with the current code

The script ran `build_task`, `verify_fix_explained`, `is_correct`, `mutate_repo` and `Rule.matches` on this repo. None of these calls use an LLM.

| Check | Result |
| --- | --- |
| Repo builds and fails with `ModuleNotFoundError: No module named 'colorutils'` | yes, same signature as the rule |
| `verify` pip_install (the stored label) | **passes** |
| `verify` `from utils import colorutils` (the rule's fix) | **passes**, and prints `(255, 255, 255)` |
| `verify` `from lib import colorutils` | **passes**, but prints `None` (the stub) |
| `is_correct` if ground truth = pip_install (as stored) | pip ✔, utils ✘ |
| `is_correct` if ground truth = fix_import/utils (the generator's convention) | pip ✘, utils ✔ (and lib ✔, see note 3) |
| Current `mutate_repo` on a `needs_local_import` example | returns `[]`, so this repo **cannot be produced today** |
| Current `mutate_repo` on a `missing_package` example | 4 decoys (utils, lib, helpers, vendor), as designed |
| Production `rule_4876b859` / `rule_6169da8c` fire on this repo | no / no (both scopes exclude it) |
| Regression suite flags the original unrepaired fix_import draft | yes, this twin alone condemns it |

### Is the label correct?

**Not under the project's own ground-truth convention.** The verifier can't settle it, because all three candidate fixes exit 0. That makes the stored label a judgement call, and the only basis for it is the 3 unanimous Solver runs. The rest of the project resolves this kind of case the other way:

- The generator labels "`utils/colorutils.py` exists, `main.py` does `import colorutils`" as `fix_import` → `from utils import colorutils` (`generator.py`, `needs_local_import`). This twin is exactly that repo plus an extra stub in `lib/`.
- Only the `utils` fix produces the real result. `lib` gives `None`. `pip_install` is simulated (every attribute returns `None`), so passing the verifier doesn't show it computes anything.
- The decoy operator's purpose (`mutate_repo` docstring) is to make a *missing-package* repo ambiguous. On a repo that already has the real local module, the comment at `audit_backend.py:92-93` says adding a second one "would make the right answer ambiguous". That's why it now refuses.

So the record is at best ambiguous and at worst wrong. It shouldn't act as a hard regression constraint.

## How the repo was produced

The repo is a `mutate_repo` output applied to one of `rule_4876b859`'s own examples (`needs_local_import`, which already contains `utils/colorutils.py`):

- The stub body `def hex_to_rgb(*args, **kwargs):\n    return None\n\n` and the `why` text "a local lib/colorutils.py exists in a subdirectory" match `mutate_repo`'s templates exactly (`audit_backend.py:96,100`).
- The current guard (`audit_backend.py:92-93`) returns `[]` for any repo that already has `{cap}.py` locally, so the current code cannot emit it.
- **Timeline:**
  - `twins.jsonl` last modified 2026-10-01 15:23.
  - Audit reports written 15:48.
  - `audit_backend.py`'s *first and only* commit (`00d8529`) at 15:52.
- Round 1 of the same report still contains mutation twins built on `utils/colorutils.py` examples (helpers, vendor, lib), so the guard wasn't there when the report was produced.

**Conclusion (inference):** an earlier, uncommitted `mutate_repo` without the "local module already exists" guard produced this twin. The guard was added afterwards, between 15:48 and 15:52, apparently in response to this problem. The exact earlier code can't be recovered from git because it was never committed.

## Consequences today

- **Production safety:** no impact. Neither production rule fires on this repo. The CI regression check skips it because it's out of scope for both rules. `check_registry` passes.
- **`rule_4876b859`'s scope has an arbitrary clause.** `file_absent lib/{0}.py` exists only to exclude this twin. The same repo with the stub under `helpers/` or `vendor/` **still fires the rule** (checked). Those variants were INCONCLUSIVE in round 1 because the Solver runs disagreed. Under the generator's convention the rule's answer there (`from utils import ...`) is correct, so the clause costs coverage, not safety: a `lib/` case falls back to the Solver.
- **Future audits are biased.** Any fix_import rule that matches this repo will be marked broken by the regression replay, and a pip_install rule that fires here would pass it. That's backwards under the generator's convention.
- **Experiment results:** no impact. The `tasks_v1`/`tasks_v2` streams contain no `lib/` repos.

## Side findings

1. **The verifier can't discriminate this family.** It only checks exit code 0, so simulated `pip_install`, `from utils` and `from lib` all "work". This is the documented reason the project measures false compilation against ground truth. It also means Solver-labelled twins on repos with several importable candidates have no independent check.
2. **Regression twins carry no provenance.** There's no operator version, generator version or label source, so stale records like this one can't be identified mechanically.
3. **`is_correct` is loose for `fix_import`.** It compares only `type`, so `from lib import colorutils` counts as correct when the truth is `from utils import colorutils` (`solver_tools.py:198-204`).

## Recommendation

1. **Fix the twin record** (needs your decision). Either:
   - (a) **remove it from `tests/regression/twins.jsonl`**, on the grounds that its label rests only on Solver unanimity on a repo the current operator deliberately refuses to generate; or
   - (b) relabel `correct_fix` to `fix_import` → `from utils import colorutils` and mark the source as relabelled.

   (a) is the conservative choice. (b) keeps a useful near-miss case, but a human then has to decide the right answer. Either way, record why in the commit.
2. **Re-audit `rule_4876b859`, yes, but it isn't urgent for safety.** Do it after step 1, because its only repair was driven by this record and its `lib/` clause is arbitrary. Expectations:
   - The current `mutate_repo` yields no mutation twins for this rule's examples, so a re-audit will rest on regression replay, controls and LLM twins.
   - A changed scope changes the fingerprint, so registration bumps it to version 2 (demote, then re-register).
   - `test_ci_checks::test_your_real_registry_state_passes_the_gate` should still pass, but anything pinned to the current scope needs re-checking.
   - It costs LLM tokens: the Solver runs k=3 times per twin.
3. **Follow-ups worth considering**, separate changes and not done here:
   - Store provenance on regression twins (`source`, operator or generator version, `label_source: solver|auditor|human`).
   - Require an Auditor or human vote, not Solver unanimity alone, before a *mutation* twin becomes a permanent regression record.
   - Let `verify` optionally compare stdout against the original program's intended output, so a stub module and a simulated install stop "passing".
   - Tighten `is_correct` for `fix_import` to compare the imported module path.
