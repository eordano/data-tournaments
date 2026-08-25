# Context evolution upgrade — 2026-08-16

## Shipped

- GEPA upgraded from 0.1.1 to 0.1.4; DSPy remains on the latest stable 3.2.1.
- Human labels are domain-scoped, pair-deduplicated (including reversed A/B
  pairs), and deterministically split into train, validation, and untouched
  holdout partitions.
- GEPA receives per-trajectory scalar scores plus actionable textual feedback.
- Kimi K3 runs the judge, GLM 5.2 performs reflection, and Claude Opus 5 curates
  discoveries into incremental, deterministic context-playbook entries.
- The production prompt is preserved as the seed. Candidate creation requires a
  positive holdout delta, no exact-accuracy regression, and no validity
  regression. Plateaus and regressions retain production.
- Each run saves its seed, GEPA candidate, curated candidate, paired outcomes,
  split digest, models, budget, and GEPA search statistics.
- Prompt studio exposes all three model roles, explicit 24/40/80-call budgets,
  split sizes, seed-to-candidate holdout scores, decision, and artifact metadata.

## Live run

Domain: `unity-explorer-bugs-luna-e2e`

- Data: 7 human judgements → 3 train / 2 validation / 2 holdout
- Requested budget: 16 metric calls; actual GEPA calls: 18 (bounded iteration
  overshoot)
- Search: 3 candidates, 3 full validation evaluations
- Validation: seed 0.50, best 0.50; GEPA conservatively selected seed index 0
- Curator: 10 structured playbook entries
- Holdout: 0.00 → 0.50; exact 0% → 50%; one improved, one unchanged, zero
  regressed, zero invalid
- Decision: accepted as local prompt candidate v2; production v1 was not moved
- Artifacts:
  `/tmp/data-tournaments-unity-e2e-20260816/optimizer/judge-instructions-unity-explorer-bugs-luna-e2e/20260816-181512-0-7ba2c26e`

## Verification

- Real DSPy `MatchJudge` + GEPA 0.1.4 deterministic adapter smoke passed.
- Full Python suite passed (live-marked tests skipped by default).
- Phoenix: 69 tests, 0 failures.
- `mix precommit` passed.
- `nix flake check --no-build` passed for the host system.
- Live `GET /prompts` returned 200 and rendered Curator, Budget, Evolve context,
  and candidate v2.
