# Operator environment v13 — six UX findings from live usage (wave-13)

Status: CONTRACT. Source: user walkthrough of :4111 (2026-08-18).

## 1. /results — revisable judgements (append-only)

"Go back and edit judging" NEVER mutates or deletes score rows. Model:
- New table judgement_revision {id, pending_id, previous_rating_id,
  new_rating_id, revised_by, reason, created_at} — immutable triggers
  (approval_event precedent).
- Revising = writing a brand-new rating (new rating_id, fresh score rows
  via the existing write path against the SAME pending, which stays
  'done') + one revision row linking old -> new.
- Effective verdict = the latest rating in the revision chain; /results
  renders the chain (original struck through / history expander).
- Downstream outcomes already derived from the old verdict are NOT
  rewritten; if the pair advanced a bracket, show an honest
  "revised after use — downstream unaffected" note.
- UI: each /results row (human ratings only) gets "Revise" -> reuses the
  SAME wheel/axis/subject UI (judge components) in a revision context,
  prefilled with the prior verdict; submit requires a reason.

## 2. IA: "Environment" = Catalog + Prompts (+ rubrics/pipelines/policies)

"Prompts" alone is too basic; catalog + prompts are one thing: the
ENVIRONMENT a campaign runs in. One /environment LiveView with tabs:
  sources (catalog projects/sources/evidence counts) · prompts (versions)
  · rubrics (eval_templates: kind/subjects/wheel summary) · pipelines
  (registry + domain bindings) · policies (approvers; NAMES only, never
  secret values).
Old routes /catalog and /prompts REDIRECT (301-style push_navigate on
mount) to /environment?tab=sources / ?tab=prompts — deep links keep
working. Primary nav: replace the two entries with one "Environment".

## 3. /brackets — demote

Direct bracket construction is plumbing, not an operator job. Remove
from primary nav; route stays alive (deep links, debugging) with a
header note "advanced/legacy — normal entry is Start -> generate ->
judge". No feature work.

## 4. /campaigns/:name — the exploration hub

Campaign detail currently dead-ends. It must link everything the
campaign touched, each section rendering gracefully when empty:
  objective/base-SHA/created header · findings by state (link: campaign
  intake evidence) · lens verdicts + validation ledger counts ·
  generated WorkOrders (link to judge queue for its domains) · fix
  branches (status chips, link /branch-fixes/:id) · release runs (link
  /runs/show?id=…) · bound pipeline (from domain_pipeline via the
  campaign's domains).
Sections query by campaign linkage where it exists; where only domain
naming convention links them, derive by prefix match and SAY SO in a
subtle caption ("linked by domain naming").

## 5. /branch-fixes/:id — GitHub-style diff

Replace the single <pre> patch block with a per-file diff view:
  parse the unified diff into files -> hunks -> lines; one card per file
  (path header, +A/-D badge, collapsible); line rows with old/new line
  numbers, +/- gutter colors (green/red bg tints), hunk separators;
  monospace; escaped; 'viewed' checkbox per file (client-only state);
  file-tree sidebar listing changed paths (anchors). Diff text comes
  from the existing get_branch diff/changed_files contract — parsing is
  pure Elixir, no shell-out. Truncation cap preserved (per-file cap +
  honest chip). Empty/missing diff keeps the current honest note.

## 6. /runs/show — more logs

The workflow_run row already carries stage_results details; the UI
shows one line per stage. Upgrade to a timeline: per-stage cards with
status icon, timestamps when present, FULL detail text (expandable,
escaped, monospace), the approval audit row (approver/decision/time,
link to audit), the exact argv/queue/build labels when present in the
status JSON, and a raw-JSON toggle at the bottom (the full client
status payload, pretty-printed). No fabrication: render only fields
that exist; absent = "not recorded".

## 7. /judge — drop the aside

The w-80 <aside> (judge_live.ex:460 workspace_split sidebar) adds
nothing during judging. Remove it from JudgeLive only (other pages
keep workspace_split); judging content goes full-width, wheel centered
with more room. Keyboard hook (JudgeShortcuts) must survive the
markup change — it currently hangs off the workspace_split element.
GROUND TRUTH (live DOM, 2026-08-18): the aside contains the DOMAIN
QUEUE PICKER ("All domains / <domain> (N pending) / filtered to…") —
load-bearing. Relocate it into the judge header as a compact select +
pending-count badge; the queue-position/rating-count line can move to
a subtle caption under the header. Nothing else in the aside survives.

## Invariants

- Append-only everywhere: revisions add rows, never touch old ones.
- No secret values on any environment surface — names/presence only.
- Legacy routes keep working (redirects, not 404s).
- Rubric rendering stays data-driven: a retired rubric's stored rows keep
  rendering under the vocabulary they were judged with, and no surface
  names a rubric it has not read out of eval_template.
- Gates: pytest and mix precommit both clean; see the suites for counts.
