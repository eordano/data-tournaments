# Showcase: The Human Review Bar, automated end-to-end (BASELINE RUN)

Source artifact: (private)
(local copy: corpus/bugsweeps-2026-08/workspaces/review-rules-aug16/review-bar-report.html —
the August 16 review-comment mining report that defined the manual bugsweep
review bar: 26 review rules, contributor profiles, analyzer candidates.)

This directory is the evidence ledger for the FIRST full end-to-end run of
that workflow on the landscape platform, executed 2026-08-17 at 51ea689
(UI at 609ea21 + showcase commit). Every step is tagged with an honest
status — nothing simulated is presented as real. This is the BASELINE,
composed run: stages were driven individually and stitched; the successor
showcase (branch-fix loop, docs/showcases/branch-fix-loop/) runs the loop
with per-branch validation and developers at the end.

Status legend
  REAL LOCAL        executed for real against local state (SQLite, CAS, UI)
  REAL HUMAN        a human-shaped action performed through the real browser UI
  REAL TEMPORAL     durable workflow on the real Temporal dev server (:7233)
  FIXTURE           sanitized invented data shaped like the real corpus
  DRY-RUN           code path runs, external effect explicitly labeled absent
  CREDENTIAL-GATED  needs E2B/GitHub/UCB tokens or Linux/KVM; not run

Environment
  DATA_TOURNAMENTS_HOME=/tmp/dt-fresh · UI :4070/:4080 (PROMPT_BACKEND=local,
  DT_OPERATOR=changeme for approval) · Temporal :7233, queue hrb-showcase-20260817

## Step map: Human Review Bar -> platform automation (as run)

| # | Review-bar step (manual, Aug 2026)      | Platform automation                          | Status | Evidence |
|---|------------------------------------------|----------------------------------------------|--------|----------|
| 1 | Collect code/Sentry/feedback signals     | sentry_csv/slack_csv/github_autoclosed adapters | REAL LOCAL (FIXTURE inputs) | artifacts/campaign-ledger.json, shots/01,02 |
| 2 | Dedup vs open PRs / prior campaigns      | campaign_intake dedup gate                   | REAL LOCAL (but see L1)     | 2 findings stopped at gate |
| 3 | Candidate dossiers w/ evidence           | findings + finding_evidence (6 refs)         | REAL LOCAL | shots/03 |
| 4 | CONFIRM/REFUTE review lenses             | review_lens_verdict, append-only             | REAL LOCAL | CONFIRM ×2 + REFUTE→repaired |
| 5 | One repair cycle after REFUTE            | repair_of chain (single cycle enforced)      | REAL LOCAL | ledger "REFUTE→repaired" |
| 6 | RED/GREEN validation w/ intended counts  | validation_ledger                            | REAL LOCAL (declared counts; no sandbox execution) | RED 3/3 GREEN 5/5 + 2 guards |
| 7 | Implementation proposals                 | WorkOrder generation, cited_evidence stamped | REAL LOCAL | 2 WorkOrders @51ea689, 3.2–4.0KB, P0/P2 |
| 8 | Developer review bar (human judgement)   | /judge + /candidates/:id/:side permalink     | REAL HUMAN (browser) | shots/05–08,11; rating b7de4cd6… |
| 9 | Learn rules from developer opinions      | proposal→evaluate→audited promote→immutable rule | REAL LOCAL; fail-closed DENIED proven first | artifacts/review-rule-v1.json; audit #1 |
| 10| Ship decision                            | Temporal release wf + audited approval + manifest | REAL TEMPORAL; promote labeled DRY-RUN | shots/09,09b,10; artifacts/audit-trail.json, release-manifest.json (sha256 b1660ed3…) |
| 11| PR / Unity Cloud Build / canary          | shipping.py contracts                        | CREDENTIAL-GATED (by design) | — |

## Run ledger

- Intake: 6 evidence refs (2 sentry, 2 slack, 2 autoclosed); 4 findings
  created; 2 deduped at the gate; slack duplicates folded no_go(stale-signal).
- Primary dossier expl-4102: confirmed_validated, root cause traced, 3
  lenses with one repaired REFUTE, RED 3/3 GREEN 5/5 + 2 guards.
- Generation: REAL pipeline (PROMPT_BACKEND=local), 2 WorkOrders with
  Goal/plan/acceptance markdown + links + cited_evidence stamped at 51ea689.
- Judgement: submitted through real Chrome (CDP) — b-clearly-better @ high,
  rationale citing the review bar; pending row 5 → done.
- Learning loop: promotion DENIED with no policy (fail-closed, live), then
  policy rule:* → audited promote → immutable retry-paths-log-and-guard v1
  with dissent preserved verbatim.
- Release: all 9 stages ok on real Temporal; audited approval event #2
  written BEFORE delivery; promote output says "[DRY-RUN — no shipping
  credentials; nothing was deployed]"; manifest digest b1660ed379c1e79e….

## Screenshots (shots/)

01 landscape · 02 campaigns · 03 campaign ledger (INDEX shape) · 05 review
queue · 06 WorkOrder full document · 07 candidate permalink · 08 judgement
submitted · 09 run awaiting approval · 09b approval submitted · 10 run
complete (promoted) · 11 judgement recorded · 12 data inspector

## Limitations found by this run (all real bugs/gaps, tracked)

L1 Dedup token extraction over-broad: matched stopwords ("does",
   "timestamp") from PR titles — the 2 dedup verdicts are not trustworthy
   until tokens are restricted to issue/branch/slug identifiers.
L2 Temporal assemble_context skipped all 4 sources (adapter configs are
   not persisted on catalog source rows; collectors got empty config) and
   fell back to canned spike data — honestly reported in stage detail.
L3 generate_workorders inside the workflow ran as stub: the client has no
   --domain flag to pass a generation domain. The real WorkOrders came
   from step 7 outside the workflow.
L4 UI approval delivery failed AFTER the audit row was written (shell-out
   python3 lacks temporalio). Audit-before-delivery held exactly as
   designed; decision re-delivered via the spike venv client. Fix:
   DT_RELEASE_CLIENT_CMD must point at a Temporal-capable interpreter.
L5 /runs/:id 400s for colon-bearing workflow ids when fetched directly
   (Plug.Static rejects the segment); LiveView click-through works.
L6 One generation run enqueued 4 identical pending pairs ("enqueued 1
   pair" logged); duplicate-row bug to investigate.
L7 Step 6 validation counts here are DECLARED (dossier protocol), not
   sandbox-executed — real execution is the successor showcase's job.
L8 This run compared two DIFFERENT bugs' WorkOrders in one pair — fine
   for prioritization, wrong for fix-vs-fix; successor showcase validates
   each fix branch in isolation.

## Provenance

Repo HEAD during run: 51ea689 (evidence stamped at that commit) on top of
609ea21. Data root /tmp/dt-fresh. Temporal workflow
release:unity-explorer:51ea689, run 01a00f5e-4de5-7e7b-b1b5-7487cb8f7129.
Judge domain hrb-release-reliability (id 2), artifact=work-order.
Fixtures: invented, sanitized (no real Sentry/Slack/user data).
