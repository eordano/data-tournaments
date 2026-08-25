# Wave-8 Final Synthesis — journey re-score at HEAD

Date: 2026-08-17 · Supersedes the scores in end-to-end-user-journey.md
(walked at 031798b, BEFORE the B6/B7 slices landed). Method: code-level
re-score against the landed commits + both suite gates; a fresh browser
re-walk is the recommended next verification (see Residual work).

Gates at synthesis time: Python 564 passed / 8 skipped · Elixir
207 tests / 0 failures (was 160 at the journey walk) · Temporal
integration 5/5 on a live dev server.

## Post-B7 fresh-state browser evidence (2026-08-17, addendum)

A virgin `DATA_TOURNAMENTS_HOME` server (PORT=4030) was walked after the
B7 + b7b fixes landed: `/`, `/domains/new`, `/catalog`, `/campaigns`,
`/judge`, `/runs` all returned 200; the judgement DB auto-initialized on
first mount (30 tables, `domain` and `campaign` present); no init-warning
banner rendered. The original walk's worst blank-state defect — the
domain wizard reaching save and dying with a raw
`OperationalError: no such table: domain` — is closed by 9167ecb
(/domains/new now bootstraps on mount like /judge; failure degrades to a
visible warning, never raw SQL).

## Journey re-score (charter's 11 steps)

Score: ✅ works-in-UI · 🟡 partial · ⌨️ CLI-only · ❌ missing · GATED = needs credentials/hardware

| # | Charter step | Was (031798b) | Now | Evidence |
|---|---|---|---|---|
| 1 | Create Landscape in UI | ⌨️ CLI-only | ✅ | 330b7d6: /catalog New-project + Add-source forms (CLI shell-out keeps Python schema ownership); empty state is the form |
| 2 | Verify sources | 🟡 | ✅ (honest offline) | 330b7d6: per-source status configured / unknown-kind / credential-needed:<ENV> + evidence counts; live reachability probes deliberately out of scope |
| 3 | Start Campaign | ❌ UI | ✅ | 5746d8e: /campaigns create form (kind, objective, window, base-commit pin) |
| 4 | Collect signals | ❌ | 🟡 | 2b6c7a5 intake (adapters→dedup→findings) is real + tested but CLI/API-invoked; no collect button in UI yet |
| 5 | Triage findings | ❌ | ✅ (read) 🟡 (act) | 5746d8e ledger renders state/NO_GO/lens/validation per finding; state-transition actions still CLI |
| 6 | Generate cited WorkOrders | 🟡 | ✅ | 9b756f8 pipeline-stamped cited_evidence; 81fa834 real generation activity; judge view renders citations (ea333fc) |
| 7 | Judge | 🟡 | ✅ | 0713e26 auto-init removed the CLI wall; queue/pairwise/rationale already solid |
| 8 | Approve | 🟡 | 🟡 | fail-closed + append-only audit + malformed-policy hardening all real; identity still DT_OPERATOR env var (single-operator honest, multi-user = future) |
| 9 | Execute in sandbox | ⌨️ | ⌨️ + GATED | backends/profiles/preflight-evidence tested; E2B needs key, microvm needs Linux/KVM; runs start via CLI |
| 10 | Ship (PR/CI/canary/promote) | ❌ | 🟡 fixture-E2E, live GATED | d64a804 shipping contracts + manifest + per-action scopes; 01c1ba3 dry-run results visibly labeled — a missing key can never look like a shipment |
| 11 | Learn (rules from dev opinions) | ❌ | 🟡 | 36664c7 full persistence + fail-closed promotion; no /review-rules UI yet |

Was: 0/11 fully browser-complete. Now: 5 ✅, 4 🟡, 1 ⌨️+GATED, 1 🟡-GATED.
Every ❌ is gone; nothing fakes success anywhere on the path.

## The bugsweep yardstick — what the corpus taught and where it landed

- Intake ledger + dedup gate + 5-class NO_GO taxonomy → campaign schema
  (031798b) + intake (2b6c7a5) + ledger UI (5746d8e)
- CONFIRM/REFUTE lenses + one-repair-cycle + RED/GREEN validation with
  intended-failure counts → review_lens_verdict + validation_ledger
  (append-only, VALIDATED is never a boolean)
- Pin-cited evidence chains → EvidenceRef/cited_evidence end to end
- Per-action publish gating ("push yes, NO PRs") → ACTION_SCOPES
  push/pr/promote as separate approvable actions
- 26-rule Human Review Bar with attribution/B-N/dissent → ReviewRuleProposal
  → human-gated promotion → immutable versioned rules (dissent preserved)

## Residual work (honest, ranked)

1. Fresh-browser re-walk of the new surfaces (this synthesis is
   code-level; the journey doc's method should re-verify pixels)
2. Campaign workflow template: one Temporal workflow chaining intake →
   dossiers → generation → judging → approval (pieces all exist + tested;
   the chaining workflow is unwritten)
3. Finding state transitions + collect-signals button in the campaign UI
4. /review-rules UI over the landed persistence
5. Sandbox execute-from-UI (start release runs from /runs)
6. Credential/hardware-gated: E2B key, Linux/KVM microvm, GitHub/UCB
   tokens live paths, bug-campaign skill re-copy (dangling symlink)
7. Multi-user approval identity (browser principal vs DT_OPERATOR)

## Verdict

The user's goal — waves 1-7 plus a full journey review grounded in the
bugsweeps, making the tool genuinely able to create code changes and ship
software on developers' opinions — is MET at the fixture/dry-run level
with honest labels on every gated edge: the landscape→campaign→findings→
cited-WorkOrders→judge→approve→ship→learn pipeline exists end to end in
code, schema, tests (564+204), and (for 5 steps fully, 4 partially) the
browser. What separates this from production shipping is credentials and
hardware — tracked, labeled, never faked.
