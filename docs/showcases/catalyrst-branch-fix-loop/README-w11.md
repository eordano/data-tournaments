# Wave-11: the branch-fix loop, live-agent authored, strict causal order

SUPERSEDES the wave-10 conclusion below/in git history. Same repo
(https://github.com/eordano/catalyrst @ 009d0fe), same defect, but this
run closes every honesty gap wave-10 disclosed:

  wave-10 limitation                ->  wave-11 status
  ─────────────────────────────────────────────────────────────────────
  branches were FIXTURE patches     ->  candidate A AUTHORED BY A LIVE
                                        CODING AGENT (CommandBackend,
                                        claude CLI) from the judged
                                        WorkOrder
  'shipped' meant release-started   ->  'shipping' -> completion sync ->
                                        'shipped' ONLY after the workflow
                                        finished (rolled-back refuses
                                        re-ship without fresh approval)
  harness protected scripts only    ->  transitive + manifest protection;
                                        C tampered an INNER TEST SOURCE
                                        and was refused pre-execution
  counters trusted as printed       ->  counts PINNED via --expected
                                        red=1/1,green=30/30,guard=5/5
  fix premise self-audited          ->  EXTERNAL ORACLE: upstream
                                        @dcl/hashing fixtures integrated
                                        into GUARD (5 = 2 exact-length +
                                        3 oracle parity)
  approval needed CLI re-delivery   ->  one-click: audit + confirmed
                                        delivery in a single browser
                                        click (retry-without-second-audit
                                        on failure)
  lineage loosely ordered           ->  STRICT causal order, enforced
                                        workorder_ref resolution

Status legend: REAL LOCAL · REAL HUMAN (browser) · REAL AGENT (paid
tokens) · REAL TEMPORAL · FIXTURE (labeled) · DRY-RUN · CREDENTIAL-GATED.

## The run (campaign base b1601397; every row DB-asserted in run.json)

1  INTAKE [REAL LOCAL, FIXTURE signals]  3 sentry rows -> 3 findings,
   3 frozen evidence refs.
2  DOSSIER [REAL LOCAL]  CONFIRM lens (real code inspection + oracle
   note) -> confirmed_validated.
3  WORKORDERS [REAL LOCAL]  domain catalyrst-cid-w11-workorders,
   2 generated, 1 pair enqueued.
4  JUDGEMENT [REAL HUMAN]  browser vote (b-clearly-better @ high) —
   winner: 'Fix is_canonical_cid: enforce exact CIDv1 decoded-length
   semantics'. The JUDGED WorkOrder is the authoring input.
5  AUTHORING [REAL AGENT + FIXTURE]  bin/branch_author.py, all three
   candidates from ONE base (same-base asserted, 0 merges each):
     A fix/w11-cid-exact-length   COMMAND backend — live claude agent.
       The agent went STRONGER than wave-10's fixture: structural
       multihash decode (varint version/codec + multihash header +
       declared digest length) instead of a hard-coded 59 — resolving
       the scoping caveat both prior audits flagged. head 3d2b3b27.
     B fix/w11-cid-min-length     FIXTURE — plausible lower-bound-only fix.
     C fix/w11-cid-harness-tamper FIXTURE — tampers the INNER guard test
       source (harness_guard.rs), not the scripts: probes wave-11's
       transitive protection.
6  VALIDATION [REAL LOCAL, pinned counts, real cargo + oracle]:
     A validated  RED 1/1 GREEN 30/30 GUARD 5/5 (harness digest recorded;
       9 protected paths: 3 script + 6 manifest-glob incl. all three
       inner test sources)
     B failed     GUARD 3/5 — passes the repro, fails both oversized
       guards (the trap for repro-only review)
     C REFUSED pre-execution — HARNESS-TAMPERED: crates/catalyrst-hashing/
       tests/harness_guard.rs; sentinel absent (nothing executed).
       Wave-10's script-only protection would have MISSED this.
7  SHIP GATE [REAL LOCAL]  refusal matrix pre-approval: all three refused
   (not-approved / failed / failed) across 5 gates incl. the new
   no-ship-in-progress + not-rolled-back.
8  DEVELOPER AT THE END [REAL HUMAN]  /branch-fixes scorecards: A shows
   the agent's patch + COMMAND provenance chip + approve; B/C have no
   decision path. A approved -> review row @ 3d2b3b27 + audit event 1.
9  SHIP [REAL LOCAL -> REAL TEMPORAL]  gateway-only; canonical repo
   identity eordano/catalyrst derived from origin URL; branch ->
   'shipping' + immutable fix_branch_ship row. B/C: zero workflow runs.
10 RELEASE [REAL TEMPORAL + REAL HUMAN]  one-click approve on /runs
   (audit event 2 + CONFIRMED delivery — no banner, no manual
   re-delivery for the first time in four runs) -> 9 stages ok ->
   promote 'promoted:dry-run-3d2b3b27 [DRY-RUN — no shipping
   credentials; nothing was deployed]'.
11 COMPLETION SYNC [REAL LOCAL]  workflow done -> sync --id 1 ->
   'shipped'. The status now MEANS the release finished.

## Evidence

artifacts-w11/ (run.json, authoring, per-branch validations, refusal
matrices, judged WorkOrder, wf-final) · shots-w11/ (13 screenshots) ·
platform gates at run HEAD: pytest 685/9 clean-env (+ the --expected CLI
addition gated 96/96 targeted), mix precommit 286/0.

## Honest limitations

- Signals remain FIXTURE (invented sentry rows; sentry.invalid). Real
  signal adapters stay CREDENTIAL-GATED.
- One live-agent candidate; B and C stay fixtures BY DESIGN (they encode
  known-bad patterns a real agent should not be asked to produce).
- The agent CLI runs via the unsandboxed sibling binary
  (claude-achtung-achtung) because the seatbelt wrappers cannot nest in
  this environment; the worktree + trusted-harness containment is the
  operative isolation here.
- Shipping remains DRY-RUN; PR creation/canary CREDENTIAL-GATED.
- The release's assemble/generate stages run labeled fallbacks — the
  branch IS the release artifact in this loop.
