# GEPA v1 → SkillOpt v2: What Changed

## Overview

The existing `skill-opt` skill (v1) implements GEPA discipline with some SkillOpt ideas. `skillopt-v2` implements the full SkillOpt paper. This document tracks exactly what changed and why.

---

## What Was Already in v1

These were correct from v1 and are preserved:

| Feature | Status |
|---------|--------|
| Edit budget (Lt=4-8) | ✓ Preserved —Lt=4 default |
| Validation gate | ✓ Preserved |
| Rejected-edit buffer | ✓ Preserved |
| Protected section invariant | ✓ Preserved |
| Per-skill effect size tracking | ✓ Preserved |
| Compactness target (~920 tokens) | ✓ Preserved |
| Description-body alignment check | ✓ Preserved |
| Bootstrap initialization | ✓ Preserved |

---

## What Changed in v2

### 1. Validation Gate Strictness (Critical Fix)

**v1:** Candidate accepted if `selection_score >= current_best_score` (ties accepted)
**v2:** Candidate accepted only if `selection_score > current_best_score` (ties rejected)

**Why it matters:** SkillOpt paper explicitly requires strictly greater. Ties accepted means the gate doesn't prevent drift — a skill that scores exactly the same as the current best would be accepted, polluting the optimization history. SkillOpt found that ties almost always precede regressions.

### 2. Slow/Meta Update (New — Highest Impact)

**v1:** Had protected section concept but no epoch-boundary update mechanism
**v2:** Full slow-update pipeline at epoch boundary with validation-gated writes to `<!-- SLOW_UPDATE_START/END -->`

**Why it matters:** Ablation shows removing slow/meta costs -22.5pp on SpreadsheetBench. This is the single most impactful control in the SkillOpt paper. v1 had the protected section but never actually wrote the slow update content.

### 3. Optimizer-Side Meta Skill (New)

**v1:** No optimizer-side memory
**v2:** `meta_skill.md` content is captured and prepended to future optimizer prompts, but never shipped to the target model

**Why it matters:** The optimizer learns across epochs what kinds of edits work in this environment. The meta skill is the optimizer's own memory — distinct from the skill being optimized.

### 4. Hierarchical Merge Pipeline (New)

**v1:** Single-stage proposal → immediate ranking
**v2:** Three-stage pipeline:
  1. Per-minibatch reflection (separate failure/success)
  2. Within-type merge (failure patches merged, success patches merged)
  3. Final merge with failure-priority + ranking

**Why it matters:** Single-stage proposals can contradict themselves across batches. Hierarchical merge deduplicates, resolves conflicts, and ensures failure-driven corrections take priority over success-driven reinforcements.

### 5. Cosine LR Scheduler (New)

**v1:** Constant Lt
**v2:** Cosine decay from Lt=4 to floor=2 over epochs (with constant/linear/autonomous options)

**Why it matters:** Early epochs explore with larger edits; later epochs consolidate with smaller edits. This mirrors learning rate schedules in neural networks.

### 6. Accumulation Factor (New)

**v1:** One rollout batch → one update
**v2:** `accumulation_factor=N`: N batches reflected separately → merged into one update

**Why it matters:** Decouples execution throughput from update frequency. Can collect evidence from 3 batches before making one edit decision.

### 7. Optimizer Prompt Library (New)

**v1:** Single combined reflection prompt
**v2:** 8 distinct prompts in `prompts/`:
  - `analyst_failure.md` — per-minibatch failure analysis
  - `analyst_success.md` — per-minibatch success analysis
  - `merge_failure.md` — failure patch consolidation
  - `merge_success.md` — success patch consolidation
  - `merge_final.md` — final failure+success merge
  - `ranking.md` — top-Lt selection
  - `slow_update.md` — epoch-boundary longitudinal writer
  - `meta_skill.md` — optimizer memory writer

**Why it matters:** Each prompt has a distinct role with distinct instructions. The separation of failure and success analysis is critical — they have different goals (correct vs reinforce).

### 8. Directory Structure (Updated)

**v1:** Flat `~/.gibson/skillopt/`
**v2:** Structured `~/.hermes/skillopt-v2/` with:
  - `rejected-edits.jsonl` (buffer)
  - `effect-sizes.jsonl`
  - `optimizer-memory.jsonl` (new — meta skill history)
  - `slow-update-cache/`
  - `skills/<name>/{current,best,checkpoint-epoch-N}.md`
  - `runs/<name>-<epoch>.yaml`

**Why it matters:** Organizes optimizer memory separately from skill artifacts. Supports checkpointing and replay.

---

## Summary of Impact by Ablation (from paper)

| Control | Impact if Removed |
|---------|-----------------|
| Slow/meta update | **-22.5pp** (SpreadsheetBench) — highest impact |
| Rejected buffer | -1.6 to -4.6pp |
| Bounded LR (Lt) | -2.5 to -3.6pp |
| Gate strictness | Gradual drift (ties = silent failures) |
| Hierarchical merge | Inconsistent edits, self-cancelling proposals |
| Meta skill | Optimizer repeats same mistakes across epochs |

---

## v1 → v2 Migration Path

1. **Keep v1 running** until v2 is validated
2. **Bootstrap v2 directories** alongside v1 (`~/.hermes/skillopt-v2/`)
3. **Validate on one skill** before rolling out fleet-wide
4. **Compare effect sizes** — v2 should show higher per-skill improvement
5. **Deprecate v1** after 2 successful v2 cycles

**The skills being trained remain the same.** The optimization discipline changes, not the artifact format. `best_skill.md` from v2 is still a valid SKILL.md for Hermes — nothing in the skill format itself changes.
