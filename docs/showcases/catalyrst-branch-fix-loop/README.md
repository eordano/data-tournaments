# Walkthrough: branch-fix loop on a REAL repo — eordano/catalyrst

Mandate (artifact 1c5679a7): a team of engineers automating bugfixes with
user-devs standing at the END of the loop, and every candidate branch
validated ALONE at its exact head SHA — never a merged aggregate.
This run targets a real repository: https://github.com/eordano/catalyrst
(Rust Decentraland catalyst), upstream 009d0fe.

Status legend: REAL LOCAL · REAL HUMAN (browser) · REAL TEMPORAL ·
FIXTURE (invented, labeled) · DRY-RUN · CREDENTIAL-GATED.

## The defect (real code, real tests)

`crates/catalyrst-hashing/src/verify.rs` `is_canonical_cid()`: the CIDv1
arm accepts any base32 string with `len() >= 58`. Every sha256 CIDv1 this
crate produces (raw 0x55 leaf or dag-pb 0x70 node — 36-byte CID) is
EXACTLY 59 chars. Truncated (58) and oversized (60/90) strings pass
validation, then fail `verify_hash` downstream. Caller audit: every use
(deploy_remote_entity, snapshots, get_content, fuzz target) validates
CIDs this system itself produced — the exact-length pin is semantically
sound for this crate; a generic multiformat validator would instead parse
the multihash structurally (noted, out of scope).

## Campaign harness (committed at campaign base 42b4717)

RED   harness_red.rs    truncated 58-char CIDv1 must be rejected  (base: 0/1 — bug present)
GREEN the crate's real suite (30 tests)                           (base: 30/30)
GUARD harness_guard.rs  exact-length pin: 60/90-char junk rejected (base: 0/2)

## The run (all rows DB-asserted; screenshots corroborate, never prove)

1. INTAKE [REAL LOCAL, FIXTURE signals]: 3 invented sentry rows
   (CATA-2201/2214/2230, sentry.invalid permalinks) -> 3 findings,
   3 frozen evidence refs. artifacts/intake.json
2. DOSSIER [REAL LOCAL]: cata-2201 investigating -> CONFIRM lens (real
   code inspection, quoted above) -> validation row RED 1/1 GREEN 30/30
   GUARD 2/2 -> confirmed_validated.
3. WORKORDERS [REAL LOCAL generation]: domain catalyrst-cid-workorders
   (artifact=work-order), 2 work orders generated, 1 pair enqueued;
   HUMAN judgement submitted in the browser (a-clearly-better @ high,
   pair -> done, rating 2b423beb). shots/14-15.
4. AUTHORING [REAL LOCAL, FIXTURE backend — labeled]: bin/branch_author.py
   authored 3 candidates INDEPENDENTLY from base 42b4717
   (same-base invariant asserted; 0 merge commits each, run.json):
     A fix/cidv1-exact-length   head 99f367c6  exact-length pin
     B fix/cidv1-min-length-59  head e47f9e2b  lower-bound-only (plausible, incomplete)
     C fix/cidv1-harness-tamper head d2de896f  no fix; edits red.sh/guard.sh to echo passing counters
5. VALIDATION [REAL LOCAL, real cargo tests, trusted harness]:
     A validated  RED 1/1 GREEN 30/30 GUARD 2/2
     B failed     RED 1/1 GREEN 30/30 GUARD 0/2  <- the branch a repro-only
                                                    reviewer would wrongly ship
     C REFUSED before execution: log first line
       'HARNESS-TAMPERED: guard.sh, red.sh'; sentinel marker absent —
       C's scripts never ran. artifacts/validation-*.json
6. SHIP GATE PRE-APPROVAL [REAL LOCAL]: gateway refused ALL THREE
   (artifacts/ship-check-pre-approval-*.json — 4-gate matrix).
7. DEVELOPER AT THE END [REAL HUMAN, browser :4100]: A shows the real
   verify.rs patch + approve; B/C have NO decision controls at all.
   A approved with review-bar rationale -> review row @ 99f367c6 +
   audit event 1. shots/01-05.
8. STALENESS [REAL LOCAL]: A's head moved (8c329d02) -> gateway refused,
   ALL FOUR gates false; revalidated at 8c329d02 under the trusted
   harness (harness digest b6f9555e) -> browser re-approval -> review row
   @ 8c329d02 + audit event 2. shots/08-09.
9. SHIP VIA GATEWAY ONLY [REAL LOCAL -> REAL TEMPORAL]: branch_ship.py
   derived repo+SHA from the record (argv asserted to carry 8c329d02,
   never caller-supplied), started release:catalyrst-e2e:8c329d02… on
   queue catalyrst-ship-q1; branch -> shipped. B and C have NO workflow
   runs (asserted). artifacts/ship-result-A.json
10. RELEASE [REAL TEMPORAL + REAL HUMAN approval; DRY-RUN shipping]:
    audited approve (event 3 written by the UI BEFORE delivery; signal
    re-delivered via the temporalio venv — the UI shell-out python lacks
    temporalio, same known gap as the baseline run) -> build
    dry-run-8c329d02 -> canary dry-run:// -> monitor window -> promote
    with the explicit label '[DRY-RUN — no shipping credentials; nothing
    was deployed]'. shots/10-13, artifacts/wf-final.json
11. SURFACE WALK [REAL HUMAN]: home/catalog/campaigns/branches/runs +
    malformed ids -> friendly pages, no crash dumps. shots/20-26.

## Blockers fixed this wave (committed before the run)

  5b3d501  authoring bridge (candidates authored from one base, provenance)
  12d8a40  trusted harness (tamper refusal pre-execution, base-pinned
           scripts, harness digest) + fail-closed ship gateway (derived
           SHA, machine-readable refusals) + b0310b4 tests
  643fdfc  UI: real patch rendering + gateway ship button

## Honest limitations

- Branch authoring backend is FIXTURE (deterministic patches); the
  command backend for real coding agents exists and is tested, but this
  run did not exercise a live agent.
- `mark_shipped` flips on workflow START (prevents double-shipping the
  same approval — deliberate), so 'shipped' means 'release started', not
  'release completed'. A completion-projection is future work.
- Protected harness paths cover the script files (red/green/guard.sh);
  the .rs harness TEST SOURCES and Cargo manifests are protectable via
  the validator's protected_paths arg but were not covered by default in
  this run — C tampered the scripts and was caught; a candidate editing
  harness_red.rs directly would need the explicit list. Default-widening
  is queued.
- The Temporal run's assemble/generate stages ran honestly-labeled
  fallbacks ('assembly failed … 0 sources', 'stub: no generation domain
  configured') — release-time generation is not part of the branch-fix
  path by design (the branch IS the artifact); wiring --domain/--project
  release evidence through the ship gateway is queued.
- Lineage order: intake/dossier/workorder/judgement were completed in
  the same data home AFTER branch authoring (environment was rebuilt
  mid-run); every row coexists and is consistent, but the strict
  artifact order intake->workorder->branch was not serially enforced
  in this run.
- Real PR creation, UCB builds, canary: CREDENTIAL-GATED as ever.

## Reproduce

  scripts + configs: candidates.json (this dir), harness files committed
  at the campaign base inside the clone; run.json carries every SHA.
