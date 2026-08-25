# Fixture contract (verified by real runs before any platform code touched it)

Built by build-fixture-repo.sh into a throwaway repo (never committed here;
SHAs change per build — bind to them at run time, that's the point).

| ref                       | RED  | GREEN | GUARD | meaning |
|---------------------------|------|-------|-------|---------|
| main                      | 0/2  | 3/3   | 1/2   | bug present; fix not landed; carried-budget guard already red on main |
| fix/retry-deadline-reset  | 2/2  | 3/3   | 2/2   | Branch A — correct fix (per-attempt deadline FROM CONFIG + abort logging) |
| fix/retry-token-clone     | 2/2  | 3/3   | 1/2   | Branch B — plausible incomplete fix: RED passes (looks fixed!) but the carried-budget guard fails |

Why B is the interesting case: a reviewer looking only at "does the repro
pass now" would approve it. Only the guard suite — the encoded review-bar
rule 'per-attempt budget must come from CONFIG, not carried state'
(retry-paths-log-and-guard v1) — catches it. Per-branch isolated
validation MUST block B from the approve path while A sails through.

Both branches fork from the SAME base commit. They are NEVER merged; every
validation runs in a detached worktree pinned to one exact head SHA.

Suite conventions (consumed by bin/branch_validator.py):
  ./red.sh   -> "RED <observed>/<intended>"  (intended-failure tests passing after fix)
  ./green.sh -> "GREEN <passed>/<total>"     (behavior that must keep working)
  ./guard.sh -> "GUARD <passed>/<total>"     (regressions the fix must not introduce)
