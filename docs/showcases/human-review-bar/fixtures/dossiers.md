# Dossier: release-retry silent failure (expl-4102)

State: confirmed_validated · Campaign: hrb-showcase · Base: 609ea21

## Root cause
ReleaseRetryService.Execute clones the CancellationToken from attempt 1
into the retry target. The clone carries the already-fired timeout, so
attempt 2 aborts instantly and nothing is logged. Users believe the
release shipped; it never did.

## Signals (3 sources, deduped)
- Sentry EXPL-4102: 143 events this week, 58 users, escalating.
- Developer report (Slack): reproduction 3/3 on staging; retry counter
  advances but no release occurs.
- GitHub #5150: same defect, auto-closed by the stale bot without a fix.

## Review lenses (aug16 protocol)
- root-cause: CONFIRM — token clone traced.
- lifecycle-regression: REFUTE — sketched fix resets the token but not
  the elapsed-time budget; slow attempt 1 still starves attempt 2.
  REPAIR (one cycle): fresh per-attempt deadline from config. Re-checked
  against worktree; objection resolved. -> CONFIRM.
- ecs-struct-perf: CONFIRM — token creation is per-release, not per-tick.

## Validation (RED/GREEN with intended counts)
RED 3/3 intended failures observed · GREEN 5/5 · 2 guard tests
Harness fix: test clock made injectable (fixture fixed, logged).

## Constraints for any code change
- Per-attempt deadline must come from configuration, never carried state.
- Retry attempts MUST log start/abort with cause.
- Guard: a test that fails if a fired token is ever reused across attempts.

# Dossier: ban-time formatter raw-string leak (expl-3977)

State: investigating (root cause confirmed; NOT yet validated)

## Root cause
BanTimeFormatter.FormatRemaining falls through to the raw input string
when expiresAt is not RFC3339 — the unparsed timestamp leaks into the
ban dialog.

## Signals
- Sentry EXPL-3977: 31 events/week, 12 users, ongoing.
- Developer report (Slack): seen again 2026-08-13; matches June report.
- GitHub #5033: auto-closed as stale without a fix.

## Review lenses
- root-cause: CONFIRM — parse-failure branch returns input unmodified.

## Constraints for any code change
- Fallback must render a localized "unknown" duration, never raw input.
- Validation still owed: RED tests for the parse-failure branch.
