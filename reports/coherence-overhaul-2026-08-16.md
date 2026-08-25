# Product coherence overhaul

Date: 2026-08-16

Local app: `http://localhost:4000`

## Outcome

The app now presents one primary journey instead of seven equally weighted tools:

**Start → Domains → Review → Results → improve prompts**

Prompts, Direct brackets, and Data remain globally reachable, but their supporting or advanced roles are explicit. The overall coherence assessment improved from **6.5/10 to 8.5/10** in the follow-up audit.

## What changed

### Navigation and terminology

- `/` now opens Start; direct artifact tournaments moved to `/brackets`.
- The global navigation consistently reads Start, Domains, Review, Results, Prompts, Direct brackets, Data.
- The user-facing workflow consistently uses Generate, Review, Compare, and Improve rather than mixing fan-out, judge wheel, judgements, learn, and tournaments.
- The document title is Data Tournaments rather than Phoenix boilerplate.
- The direct-bracket creation page now includes the same global navigation and an explicit link back to the domain workflow.

### Domain lifecycle

- Each domain shows a state: Ready to generate, Human review, Model review, Results ready, or Needs attention.
- Cards show pair counts, completed/pending human ratings, completed/pending model ratings, failures, completion percentage, and last activity.
- Contextual actions lead directly to a domain-filtered Review queue or Results comparison.
- Repeated generation now allocates domain-unique match IDs so unrelated runs cannot be merged into one result group.

### Results

- `/results` groups ratings by domain and match instead of displaying a flat score log.
- Candidate A and B show title, body, and authoritative source reference side by side.
- Human, Kimi K3, GLM 5.2, and Claude Opus 5 appear in a stable role order with confidence and full rationales.
- Each group reports Human and panel agree, Human and panel disagree, No model majority, or Awaiting human baseline.
- Domain and rater filters preserve match context; exports now inherit both filters.
- Every domain result links back to domain configuration, and pending work links to Review.
- `/judgements` remains as a compatibility route to the same Results experience.

### Prompt backend

- The Elixir prompt client now follows the same backend selection as Python.
- `PROMPT_BACKEND=local` reads, promotes, and displays `${DATA_TOURNAMENTS_HOME}/prompts.json`.
- `PROMPT_BACKEND=langfuse` uses Langfuse; `auto` selects it only when both credentials exist.
- Prompt Studio, domain editing, and Data all identify the active backend and location.
- The previous false “No prompts in Langfuse” state is gone in local mode; the live page displays the actual domain prompt versions.

### Advanced tools

- Direct brackets are explicitly described as an advanced path for already-comparable artifacts, without domain candidate generation.
- Data inspector is explicitly a raw troubleshooting/export surface and points reviewers back to Results.

## Fresh frontier-panel smoke run

Domain: `coherence-frontier-smoke`

- Kimi K3 generated two source-backed DiskCache findings from one source item.
- One pair was enqueued for one human and the three-model panel.
- The human reviewer, Kimi K3, GLM 5.2, and Claude Opus 5 all selected `b-marginally-better`.
- All three model rationales distinguished silent short-read corruption from the narrower delete-before-move cache-loss window.
- The live domain card transitioned to Results ready.
- The live Results view showed both cards, `Explorer/Assets/DiskCache.cs`, all four rationales, and Human and panel agree.

## Verification

- All primary and advanced routes returned HTTP 200: `/`, `/start`, `/domains`, `/judge`, `/results`, `/prompts`, `/brackets`, `/inspect`.
- Full Phoenix suite: **69 tests, 0 failures**.
- Full Python suite: passed with **2 expected skips**.
- Additional focused post-change checks passed for repeated generation IDs, grouped Results, navigation, and domain-scoped export.
- The server remains running at `http://localhost:4000`.

## Remaining opportunities

The product is now coherent for the complete workflow. Further refinements could persist explicit generation-run records (instead of inferring lifecycle from queue rows), add cancellation controls to long-running generation jobs, and add responsive visual-regression snapshots.

