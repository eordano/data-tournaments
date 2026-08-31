# Wave 8 — From dry-run platform to shipping tool (charter)

Started: 2026-08-17 · Predecessor: unity-explorer-release-platform.md
(waves 0–7, closed at DRY-RUN level at f37f577). This charter REOPENS the
quality bar: the user's directive is that the tool must be genuinely
useful to create code changes and ship software, with developer opinions
(review comments, judging, feedback) upgrading the system itself. The
August DCL bugsweep campaigns (corpus/bugsweeps-2026-08/) are the
yardstick: a hand-run version of exactly this product that shipped 24
confirmed+validated fixes.

## Status vocabulary (used in every acceptance claim)

    IMPLEMENTED+TESTED   unit/integration tests pass
    FIXTURE-E2E          full path exercised with fixtures, no network
    REAL-E2E             exercised against real services (Temporal, browser)
    GATED                config/code complete; needs credentials/hardware
    STUB                 documented placeholder — never counted as done

## Product vocabulary (consistent everywhere)

    Landscape/Project   system under management: repos, sources, envs, policies
    Domain              judging objective/rubric (correctness, reliability…)
    Campaign            bounded effort: bugsweep, release
    Finding/Dossier     deduplicated signal cluster w/ root-cause evidence
    WorkOrder           structured, cited, actionable change proposal
    WorkflowRun         durable creation/execution/shipping process
    ReviewRule/Skill    versioned, HUMAN-APPROVED org knowledge

## The target user journey (acceptance = this works in a browser)

1. Create Landscape "Unity Explorer": connect code, GitHub, Sentry, Slack,
   UCB, instrumentation, docs, skills — from the UI, no CLI seeding
2. Verify sources: health, last sync, trust tier, credential status
3. Start Campaign (bugsweep | release): objective + time window
4. Collect signals: per-source counts + failures, no log walls
5. Triage findings: deduped dossiers w/ code/Sentry/Slack/GitHub links
6. Generate cited WorkOrders (pipeline-stamped cited_evidence)
7. Judge: Human Review Bar + developer rules, pairwise, rationale
8. Approve: authenticated principal, fail-closed policy, immutable audit
9. Execute: pinned sandbox; inspect patch + test evidence
10. Ship: PR, CI/UCB tracking, canary, monitor, promote/rollback
11. Learn: outcomes + review comments -> ReviewRuleProposal -> human
    promotion -> versioned rules; provenance + rollback retained

## Known gaps being reopened (from the honest close of f37f577)

- Landscape creation is CLI-only; Catalog UI is read-only
- No adapters: Sentry, Slack, review comments, telemetry, bugsweep corpus
- cited_evidence renders but generation never stamps it
- build/canary/promote are stubs; no PR-creation activity
- E2B unexecuted; microvm unverified (hardware)
- No campaign layer (findings/dossiers/dedup) at all
- Developer feedback has no path into rules/skills
- bug-campaign skill in corpus = dangling symlink (content NOT ingested;
  awaiting re-copy from dcl host)

## Plan of record

Track A (audits, parallel, before any build):
  A1 waves-1-7 acceptance matrix ✅ (4524e4b)
  A2 bugsweep product model from corpus ✅ (4524e4b)
  A3 real-browser journey walk — RETRY in flight (first agent hit its
     iteration cap without writing the doc; retry writes skeleton-first)

Track B (build slices, each gated + committed separately, informed by A):
  B1 cited_evidence pipeline stamping ✅ (9b756f8)
  B2 signal adapters ✅ (40c8235: sentry_csv, slack_csv, github_autoclosed,
     dedup_lists — redaction tested; corpus adapter still pending the
     bug-campaign skill re-copy)
  B3 landscape setup UX (create/edit project+sources in UI, health checks)
     — queued
  B4 campaign layer: finding/dossier schema ✅ (031798b: campaign/finding/
     finding_evidence/review_lens_verdict/validation_ledger + INDEX-shaped
     ledger rollup); campaign workflow template + ledger UI remain.
     PLUS (unplanned, from audit findings #4/#5): real generate_workorders
     activity + honest judging gate ✅ (81fa834 — auto-pass-at-0.92 stub
     replaced by generation_bridge with fail-on-abort semantics)
  B5 learning loop: ReviewRuleProposal -> human promotion -> versioned rule
     ✅ (36664c7: evidence floor, fail-closed promotion via approvals RBAC,
     immutable versioned rules, dissent preserved verbatim)
  B6 shipping activities ✅ (d64a804: GitHubShipper idempotent PR + CI
     status, UCBTracker, CanaryMonitor, release manifest, per-action
     ACTION_SCOPES push/pr/promote; live credential-gated) + 01c1ba3
     (dry-run results visibly labeled — a missing key can never look
     like a real shipment)
  B7 (from journey review, in flight): campaign UI (/campaigns list +
     ledger + create form) ∥ landscape setup UX in /catalog (create
     project, add/archive sources, honest offline source status) ∥
     quick wins (judge DB auto-init, model dropdown filtering)
  F1 final synthesis — pending (journey re-walk after B7 lands)

## Safety rails for this wave

- Corpus is TIER3 + potentially secret-bearing: never sent wholesale to
  models; excerpts redacted; 341M of JSONL streamed only for targeted
  questions, never bulk-loaded
- git add stays explicit-path — the 351M corpus/ must never be committed
- No push without user authorization; commit-as-we-go continues
- Developer attribution + minority opinions preserved in the learning loop
