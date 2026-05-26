---
name: skillopt-v2
description: SkillOpt v2 — full SkillOpt discipline for SKILL.md training. Bounded edits (Lt=4, cosine decay), validation gate (strict improvement, ties rejected), rejected-edit buffer, protected slow-update section, optimizer-side meta skill, hierarchical merge pipeline. Based on arXiv:2605.23904.
category: gibson
triggers:
  - skillopt
  - skill training
  - bounded skill optimization
  - skillopt-v2
  - text-space optimization
---

# SkillOpt v2 — Trainable Skill Artifacts

SkillOpt v2 treats SKILL.md files as the external trainable state of a frozen agent. This is an upgrade from the existing GEPA-based `skill-opt` skill, incorporating the full SkillOpt control set from arXiv:2605.23904.

**What changed from `skill-opt` v1:**
- Added `<!-- SLOW_UPDATE_START/END -->` protected section
- Added optimizer-side meta skill (never shipped to target)
- Added hierarchical merge (failure → success → final)
- Added cosine LR scheduler for edit budget
- Added accumulation (multiple batches → one update)
- Added full optimizer prompt library (8 prompts)
- Fixed gate strictness (ties now rejected, was ties-accepted)
- Added per-skill optimizer memory path

---

## Core Loop (4-Step)

```
Rollout → Reflect → Edit → Gate
```

**Rollout:** Frozen target model executes task batch with current skill → scored trajectories
**Reflect:** Optimizer model analyzes success/failure minibatches separately → edit proposals
**Edit:** Merge hierarchically, rank under budget, apply bounded update
**Gate:** Candidate accepted only if held-out selection strictly improves

---

## Skill Document Structure

Every SKILL.md has two zones separated by protected markers:

```markdown
---
name: example
description: One-line description
category: gibson
---

## Purpose
What this skill does.

## Core Principles
Durable lessons. Rarely change.

<!-- SLOW_UPDATE_START -->
## Slow Update
Protected. Written only at epoch boundary. Step-level edits CANNOT touch this.
<!-- SLOW_UPDATE_END -->

## Steps
Numbered procedure. Most frequently updated.

## Pitfalls
Failure modes discovered during use.

## Examples
Concrete snippets.

## Verification
How to confirm success.
```

**Protected (off-limits for step-level edits):**
- YAML frontmatter
- `## Purpose`
- `## Core Principles`
- Content inside `<!-- SLOW_UPDATE_START/END -->`

**Editable (bounded edits allowed):**
- `## Steps`
- `## Pitfalls`
- `## Examples`
- `## Verification`

---

## The 5 Controls

### Control 1: Edit Budget (Textual Learning Rate)

**Lt = 4** (default), floor = 2, max = 8.

```python
SCHEDULE = "cosine"   # constant | linear | cosine | autonomous
LT_DEFAULT = 4
LT_FLOOR   = 2
LT_MAX     = 8
```

Cosine decay: starts at Lt, decays to floor over epochs.

Without the budget: -2.5pp on SearchQA, -1.8pp on SpreadsheetBench, -3.6pp on LiveMath. **The budget is the learning rate. Remove it and the loop is ad hoc rewriting.**

### Control 2: Validation Gate

**Rule:** Candidate accepted only if `selection_score > current_best_score`. Ties → rejected.

This is the only mechanism that prevents catastrophic drift. Every edit is a proposal-and-test, not self-editing.

The gate runs on a **held-out selection split** — never the training set, never the test set. Training set = evidence. Selection set = gate. Test set = reported only.

### Control 3: Rejected-Edit Buffer

```jsonl
{"skill": "fleet-router", "epoch": 1, "step": 3, "edit_type": "replace",
 "proposed": "Changed routing to semantic scoring", "reason": "validation failed",
 "score_delta": -4.2, "timestamp": "2026-05-26T12:00:00Z"}
```

**Rule:** Before proposing any edit, scan buffer. If the same pattern rejected 2+ times, skip permanently.

Removing the buffer drops scores 1.6–4.6pp. The buffer is the optimizer's negative feedback loop.

### Control 4: Slow/Meta Update (Epoch Boundary Only)

At the **end of each epoch**, run the slow update:

1. Sample same tasks under previous-epoch skill AND current-epoch skill
2. Categorize: improvements / regressions / persistent failures / stable successes
3. Write longitudinal guidance to `<!-- SLOW_UPDATE_START/END -->` section
4. The update itself must also pass the validation gate

**Protected section invariant:** Step-level edits (Lt budget) CANNOT touch content inside the markers. Only epoch-boundary slow update can write there.

Removing slow/meta update: -22.5pp on SpreadsheetBench. **This is the single most impactful control.**

The **optimizer-side meta skill** is separate and is never shipped with the target skill:
- Captures: which edit patterns helped, which failed, which to avoid next epoch
- Prepended to future optimizer prompts
- Does not appear in `best_skill.md`

### Control 5: Hierarchical Merge

Edit proposals go through three stages:

```
Stage 1: Per-minibatch reflection
  - Failure minibatches → failure_patch (append/insert_after/replace/delete)
  - Success minibatches → success_patch

Stage 2: Within-type merge
  - All failure patches → merged_failure (dedupe, resolve conflicts, preserve prevalent)
  - All success patches → merged_success (dedupe, conservative)

Stage 3: Final merge
  - FAILURE PRIORITY: failure edits preserved unless they directly conflict
  - Success edits added only for patterns NOT covered by failure edits
  - Output: ranked list of ≤ Lt edits
```

Ranking criteria (in priority order):
1. **Systematic impact** — edits addressing widespread failures rank highest
2. **Complementarity** — edits filling gaps, not duplicating existing content
3. **Generality** — general principles over specific instances
4. **Actionability** — clear, concrete guidance

---

## Hyperparameters

```yaml
epochs: 4
rollout_batch_size: 40        # tasks per rollout
reflection_minibatch_size: 8  # trajectories per reflection call
accumulation_factor: 1        # batches before one update (1 = no accumulation)
edit_budget: 4                # Lt — max edits per step
edit_budget_floor: 2          # minimum Lt (cosine decay endpoint)
scheduler: cosine              # constant | linear | cosine
optimizer_reflection_rounds: 3
optimizer_model: gpt-5.5      # or target-matched
target_model: frozen           # never updated
selection_gate: strictly_greater  # ties rejected
slow_update_samples: 20        # tasks per epoch for slow update comparison
meta_skill: enabled            # optimizer-side only, never shipped
rejected_buffer: enabled       # skip patterns rejected 2+ times
```

---

## Per-Skill Effect Size Tracking

**Aggregate accuracy is the wrong unit.** Individual skills move 20–25pp; corpus average may only move 1pp. Track per-skill:

```jsonl
{"skill": "fleet-router", "epoch": 2, "phase": "accepted",
 "edits": 3, "tokens_before": 920, "tokens_after": 880,
 "selection_score_before": 71.4, "selection_score_after": 84.2,
 "test_score": 82.1, "improvement": "+12.8pp", "timestamp": "..."}
```

**Log path:** `~/.hermes/skillopt-v2/effect-sizes.jsonl`

---

## Directory Structure

```
~/.hermes/skillopt-v2/
  rejected-edits.jsonl       # rejected edit buffer (append-only)
  effect-sizes.jsonl        # per-skill effect sizes
  optimizer-memory.jsonl     # optimizer-side meta skill history
  slow-update-cache/        # epoch-end comparison results
  skills/
    <skill-name>/
      current.md            # current skill version
      best.md               # best validated skill
      checkpoint-epoch-N.md # epoch checkpoint snapshots
  runs/
    <skill-name>-<epoch>.yaml  # run receipts
```

---

## Optimizer Prompt Library

Located in `prompts/` directory:

| File | Role |
|------|------|
| `analyst_failure.md` | Per-minibatch failure analysis → patch proposals |
| `analyst_success.md` | Per-minibatch success analysis → patch proposals |
| `merge_failure.md` | Merge all failure patches → one coherent patch |
| `merge_success.md` | Merge all success patches → one coherent patch |
| `merge_final.md` | Merge failure + success patches → final ranked list |
| `ranking.md` | Rank and select top-Lt edits |
| `slow_update.md` | Epoch-boundary longitudinal guidance writer |
| `meta_skill.md` | Optimizer-side memory writer (never shipped) |

All prompts output JSON. All include the protected-section warning. All enforce the edit budget.

---

## Bootstrap (Run Once)

```python
import os, json
from pathlib import Path

BASE = Path("~/.hermes/skillopt-v2").expanduser()
(BASE / "rejected-edits.jsonl").touch()
(BASE / "effect-sizes.jsonl").touch()
(BASE / "optimizer-memory.jsonl").touch()
(BASE / "slow-update-cache").mkdir(exist_ok=True)
(BASE / "skills").mkdir(exist_ok=True)
(BASE / "runs").mkdir(exist_ok=True)
```

---

## Full Optimization Cycle

```
FOR each epoch:
  Shuffle train split into rollout batches
  Reset rejected-edit buffer (epoch-local)

  FOR each optimization step:
    FOR each accumulation step:
      Collect rollout_batch (rollout_batch_size tasks)
      Split into success / failure minibatches (size = reflection_minibatch_size)

    # Stage 1: Per-minibatch reflection
    FOR each failure minibatch:
      analyst_failure_prompt → failure_patch_i
    FOR each success minibatch:
      analyst_success_prompt → success_patch_i

    # Stage 2: Within-type merge
    merge_failure_prompt(failure_patch_1..N) → merged_failure
    merge_success_prompt(success_patch_1..N) → merged_success

    # Stage 3: Final merge + ranking
    merge_final_prompt(merged_failure, merged_success) → ranked_edits
    ranking_prompt(ranked_edits, Lt) → selected_edits (≤ Lt)

    # Apply bounded edits → candidate_skill
    candidate_skill = apply_edits(current_skill, selected_edits)

    # Validation gate
    IF skill_hash(candidate_skill) in score_cache:
      selection_score = score_cache[skill_hash]
    ELSE:
      selection_score = evaluate(target_model, candidate_skill, selection_split)
      score_cache[skill_hash] = selection_score

    IF selection_score > current_best_score:
      current_skill = candidate_skill
      IF selection_score > best_score:
        best_skill = candidate_skill
        best_score = selection_score
    ELSE:
      append rejected edit to buffer
      append to rejected-edits.jsonl

  # End of epoch: slow update
  IF epoch >= 2 AND slow_update_enabled:
    slow_update_prompt(previous_epoch_skill, current_skill, comparison_results)
      → slow_update_content
    # Write to <!-- SLOW_UPDATE_START/END --> of best_skill
    # Validate through gate again (still must pass)
    # IF fails: skip slow update, keep current best_skill

  # Optimizer-side meta skill update
  IF epoch >= 2 AND meta_skill_enabled:
    meta_skill_prompt(previous_skill, current_skill, comparison)
      → meta_skill_content
    append to optimizer-memory.jsonl
    # Prepend to future optimizer prompts (NOT in best_skill.md)
```

---

## Key Invariants

1. **Frozen model** — target model weights never change
2. **Protected section** — step-level edits cannot touch slow-update markers
3. **Gate strictness** — ties rejected, strictly greater only
4. **Rejected buffer** — skip patterns rejected 2+ times
5. **Optimizer memory** — meta skill stays optimizer-side only
6. **One exported artifact** — `best_skill.md` is all the target model ever sees

---

## Verification Checklist

- [ ] Token count ≤ 4,000 chars after edits
- [ ] YAML frontmatter intact after every edit cycle
- [ ] Protected sections unchanged (unless epoch boundary)
- [ ] Edit count ≤ Lt (never exceeds edit budget)
- [ ] Gate accepts only strictly-improving candidates (ties rejected)
- [ ] Rejected buffer checked before proposing edits
- [ ] Effect size recorded per accepted edit
- [ ] Slow update runs only at epoch boundary
- [ ] Meta skill never appears in `best_skill.md`
- [ ] `best_skill.md` ≤ 2,000 tokens (SkillOpt median ~920)

---

## References

- Paper: arXiv:2605.23904 (Microsoft, SJTU, Tongji, Fudan — May 2026)
- Repo: microsoft.github.io/SkillOpt/
- Prompt library: `prompts/` directory
- GEPA v1 comparison: `references/gepa-v1-vs-v2.md`
