# Optimizer Prompt Library

All 8 prompts from SkillOpt paper (arXiv:2605.23904) Appendix A.

## Call Order

```
1. analyst_failure.md    → per-minibatch failure analysis
   analyst_success.md    → per-minibatch success analysis

2. merge_failure.md      → consolidate all failure patches
   merge_success.md      → consolidate all success patches

3. merge_final.md        → merge failure + success → ranked pool

4. ranking.md            → select top-Lt edits

[Apply bounded edits → candidate skill]

5. slow_update.md        → (epoch boundary only) longitudinal guidance
   meta_skill.md         → (epoch boundary only) optimizer memory
```

## All Prompts Output JSON

Every prompt returns JSON. Parse before applying. Never let raw LLM output modify files directly — always validate JSON structure before applying edits.

## All Prompts Respect Protected Section

Every prompt contains the protected-section warning:
```
The skill document may contain a section between
<!-- SLOW_UPDATE_START --> and <!-- SLOW_UPDATE_END --> markers.
This is a PROTECTED section managed by a separate slow-update process.
Do NOT propose any edits that target, modify, or delete content within these markers.
```

## Patch Operations

All patch prompts support 4 operations:
- `append` — add content at end of skill
- `insert_after` — add content after specific text (requires `target`)
- `replace` — replace specific text (requires `target`)
- `delete` — remove specific text (requires `target`)

## Key Invariants

1. Slow update runs ONLY at epoch boundary — never at step level
2. Meta skill is optimizer-side only — never appears in best_skill.md
3. Ranking selects ≤ Lt edits — never exceed edit budget
4. Failure edits take priority over success edits in final merge
