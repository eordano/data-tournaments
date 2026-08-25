# Showcase: the branch-fix loop — developers at the end, every branch validated alone

Successor to docs/showcases/human-review-bar/ (the baseline composed run).
Target artifact: https://claude.ai/code/artifact/1c5679a7-a496-46a8-91e9-eb97228358f6
(the Aug-16 Human Review Bar), automated for a TEAM OF ENGINEERS doing
bugfixes, with two hard requirements from the user:

  1. USER-DEVS STAND AT THE END OF THE LOOP — the final decision on every
     fix is a developer clicking approve/reject in the UI, bound to the
     exact tested SHA, behind the audited approval gateway.
  2. EVERY BRANCH-FIX IS VALIDATED IN ISOLATION — RED/GREEN/GUARD suites
     run per branch in a detached worktree pinned to that branch's head
     SHA. No merged/aggregate tree is ever validated; merge commits in
     base..head are rejected at registration by construction.

Status legend: REAL LOCAL · REAL HUMAN (browser) · REAL TEMPORAL ·
FIXTURE · DRY-RUN · CREDENTIAL-GATED. Nothing simulated is presented as
real.

RUN EXECUTED 2026-08-17. Platform commits exercised: 05562e2 (spine),
5758b57 (UI), 95e4fbc (--id fix, found BY this run), db8da3f (L1 dedup).

## The loop as run

  fixture repo (1 bug, 2 fix branches from the same base, never merged)
    -> finding + campaign (bfl-retry) ......................... REAL LOCAL
    -> branch registration (SHA-bound, merge-free) ............ REAL LOCAL
    -> PER-BRANCH isolated validation (detached worktrees) .... REAL LOCAL
         A fix/retry-deadline-reset: RED 2/2 GREEN 3/3 GUARD 2/2 -> validated
         B fix/retry-token-clone:    RED 2/2 GREEN 3/3 GUARD 1/2 -> FAILED
         (B passes the repro — only the encoded review-bar guard
          'budget from config, not carried' catches it)
    -> DEVELOPER DECISIONS in /branch-fixes .............. REAL HUMAN (browser)
         B: approve control ABSENT + honest failure note (shots/03)
         A: approved with rationale -> fix_branch_review row bound to the
            exact tested SHA + audited approval_event
            branchfix:fix/retry-deadline-reset:<sha12> (shots/04)
    -> STALENESS: A's head advanced post-approval ............. REAL LOCAL
         refresh -> status stale; CLI approve on stale head REFUSED
         (ValueError names the new head); UI approve gone, STALE marker
         (shots/05) -> re-validated at new head (RED 2/2 GREEN 3/3
         GUARD 2/2) -> browser re-approved: second review + audit row at
         the NEW SHA (shots/06)
    -> RELEASE for approved A ONLY, commit = exact tested SHA . REAL TEMPORAL
         release:bfl-repo:0f4b6bf3df94… on fresh dev server, unique queue
         bfl-e2e-20260817; approved through /runs/show?id=… (the L5 fix,
         shots/07-08); all 9 stages ok; promote says
         "[DRY-RUN — no shipping credentials; nothing was deployed]"
         Audit trail: 3 events (A@old-sha, A@new-sha, release) — every
         decision names its exact SHA.

## Acceptance bar (each item CHECKED by this run)

- [x] Two real branches, same base, ZERO merge commits (run.json records
      rev-list --merges empty for both)
- [x] Isolated detached worktrees; main worktree untouched
- [x] Append-only validation rows, each naming ONE tested_sha
- [x] B (guard-failing) had NO approve path in the UI, and Python refuses
      approve without a current passed validation (fail-closed, tested)
- [x] A approved by a developer in the browser; audit row before anything
- [x] Head change invalidated the approval: stale + refused + UI blocked
      until re-validation at the new SHA
- [x] Temporal ran for the approved branch only, pinned to the tested SHA
- [x] DRY-RUN labels on every unshipped external effect
- [x] Gates at evidence time: pytest 602/8 · mix precommit 251/0 ·
      live Temporal run to completion

## Evidence

  shots/01 branch list · 02 A validated (approve enabled) · 03 B failed
  (approve ABSENT + note) · 04 A approved · 05 A stale after head change
  (approve gone) · 06 A re-approved at new head · 07 release awaiting
  approval via /runs/show?id=… · 08 release approved

  artifacts/run.json (git SHAs, merge checks, full DB dump, artifact
  hashes) · validation-A/B.json + validation-A-revalidated.json ·
  branch-A/B-final.json · release-final-status.json

## Bugs found BY this run (the loop caught real defects)

1. UI review shell-out missed --id: browser Approve looked fine on
   screen but wrote NO rows (argparse exit 2 only in the flash). Fixed +
   argv pinned in test (95e4fbc). Lesson encoded in the decision script:
   screenshots are never proof — assert DB rows.
2. Baseline L1 dedup false-positives fixed + regression-tested (db8da3f).

## Limitations (honest)

- Fix branches are deterministic FIXTURE patches, not model-generated
  (FIXTURE_AUTOFIX_BACKEND); the WorkOrder->branch authoring step is the
  next slice.
- ~~The release workflow's assemble/generate stages ran in stub mode~~
  CLOSED by the E4 re-proof (below) after wave-9 B4 (0c1a513).
- Shipping stages remain CREDENTIAL-GATED (by design): promote is labeled
  DRY-RUN; no PR/UCB/canary side effects exist.
- Local Chrome one-shot screenshots are sandbox-dead; evidence captured
  via CDP + Playwright against the real running UI.

## E4 addendum: live re-proof after the B4 blocker fixes (0c1a513)

Run release:bfl-repo:e4proof4 on real Temporal (queue bfl-e4-q3), started
with the NEW `--domain bfl-release-reliability` flag against project bfl
carrying 2 frozen evidence_ref rows and an empty live source config:

    assemble_context:     ok — "assembled from catalog project 'bfl'"
                          (L2: frozen evidence recovered; NO 'skipped',
                           NO spike-data fallback)
    generate_workorders:  ok — "generated 2 work orders, enqueued 1 pairs,
                          0 failures" (L3: REAL pipeline inside Temporal,
                          stub eliminated)
    judging_gate:         ok — score=1.0 (honest ratio, not auto-pass)
    approval -> promote:  audited approve by esteban; promote says
                          "[DRY-RUN — no shipping credentials]"

    L6 live check: 1 pair logged == exactly 1 pending row, rater_type
    'human' (was 4 identical rows in the baseline run).

The failed attempts are preserved as evidence too (honest ledger):
e4proof/e4proof2 rolled back on "generation stack unavailable" (worker
venv lacked dspy — resolved by PYTHONPATH-ing the nix site-packages into
the worker env), e4proof3 rolled back on the L6 fail-loud guard ("no
active human job_configuration — run judgement init"), proving the gate
fails closed instead of faking success. Artifacts:
artifacts/e4-release-with-domain.json + artifacts/e4-db-proof.json
(pending-row proof, frozen digests, audit events, all 5 run statuses).

## Reproduce

  bash docs/showcases/branch-fix-loop/build-fixture-repo.sh /tmp/bfl-repo
  bash docs/showcases/branch-fix-loop/run-e2e.sh /tmp/dt-branch-e2e /tmp/bfl-repo
  # then: UI decisions at /branch-fixes, release via bin/release_workflow
  # (RELEASE_TASK_QUEUE unique per run; see runbook)
