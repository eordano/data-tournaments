# End-to-End User Journey Review (Wave-8 A3)

Date: 2026-08-17 · Commit: 031798b · Method: live Phoenix app on
`http://localhost:4020` (PROMPT_BACKEND=local, fresh
DATA_TOURNAMENTS_HOME=/tmp/dt-journey2), blank-state pass first, then a
seeded pass (catalog project + source, awaiting-approval workflow run,
campaign + finding in DB), then resilience spot-checks. Every finding is
severity-tagged: **BLOCKER** (journey step impossible), **MAJOR**
(possible only via CLI or with serious friction), **MINOR** (works but
confusing), **POLISH** (cosmetic).

Journey definition: `docs/plans/wave-8-shipping-tool.md` §"The target
user journey" (11 steps, acceptance = works in a browser).

## Verdict (TL;DR)

**0/11 charter steps fully work in the browser** (4 partial · 3
CLI-only · 4 missing). No crashes anywhere — resilience is strong — but
the campaign layer (steps 3–5) has live schema + data and **zero UI**,
landscape creation (step 1) is CLI-only by design of its own empty
state, and approval identity is a server env var. Full verdict at the
end of this doc; 15 tagged findings (3 BLOCKER, 6 MAJOR) below.

## 1. Blank-state pass (new user, empty home dir)

All 13 registered routes return HTTP 200 on a completely fresh home dir;
nothing crashes blank. Router (`ui/lib/tournament_ui_web/router.ex`) has
exactly: `/`, `/start`, `/brackets`, `/new`, `/judge`, `/results`,
`/judgements`, `/prompts`, `/domains`, `/domains/new`,
`/domains/:name/edit`, `/catalog`, `/catalog/:project`, `/runs`,
`/runs/:workflow_id`, `/inspect` (+ download/export controllers).
**There is no campaign route of any kind** — `/campaigns` → 404.

| Route | What a new user sees | Empty state quality |
|---|---|---|
| `/`, `/start` | "Start with one question" onboarding: 5-step explainer (Source → Generate → Review → Compare → Improve), 5 starter categories + custom, link to direct brackets | ✅ Best page in the app; real funnel with CTAs |
| `/domains` | "No domains yet" + explainer + **"Choose your first evaluation category →"** CTA + "+ New domain" | ✅ CTA-driven empty state |
| `/domains/new?starter=correctness` | 2-step wizard, lens pre-filled from starter, name/goal/corpus (inline · filesystem · sqlite), "Draft prompts →" | ✅ Genuine in-UI creation path |
| `/catalog` | "No projects in the catalog yet. Projects are registered from the command line: `python3 bin/catalog.py create-project`" | ⚠️ Dead wall — the empty state itself tells you to leave the browser |
| `/judge` (and `?domain=x`) | "Inbox zero" + banner "fabric DB missing — run `bin/judgement.py init`" | ⚠️ Graceful, but remedy is CLI |
| `/results` | "Judgement database has not been initialized yet", 0 ratings/0 matches, CTAs to Domains & Review queue | ✅ CTA-driven |
| `/runs` | "No workflow runs recorded yet. Runs appear when a release workflow starts: `python3 -m bin.release_workflow.client start`" | ⚠️ Dead wall — CLI is the only ignition |
| `/prompts` | Prompt studio, backend = Local store, empty-store hint "Create a domain or initialize the judgement fabric" | ⚠️ See F-6: model dropdowns are polluted |
| `/brackets` | "No direct brackets yet. Click + new" | ✅ CTA |
| `/new` | Full direct-bracket builder: server file browser, drag-drop upload, DB-query source, JS-transform source, match-prompt presets | ✅ Most capable creation UI in the app |
| `/inspect` | Read-only counts (domains 0 / pending 0 / scores 0), JSON/CSV export, pointer to Results | ✅ Honest advanced view |
| `/campaigns` | **404 — no such route** | ❌ Whole campaign layer absent from UI |

### Blank-state findings

- **F-1 · BLOCKER (charter step 1).** Landscape/catalog creation is
  CLI-only and the `/catalog` empty state *says so on screen*
  ("registered from the command line: python3 bin/catalog.py
  create-project"). The charter's step 1 explicitly requires "from the
  UI, no CLI seeding". The browser flow dies at its very first step.
- **F-2 · BLOCKER (charter step 3).** No campaign UI exists at all: no
  route, no nav entry, no mention of campaigns anywhere in the rendered
  pages. `/campaigns` 404s.
- **F-3 · MAJOR.** `/runs` cannot start a run; empty state hands the
  user a `python3 -m bin.release_workflow.client start` incantation.
  Steps 9–10 of the journey can only be *observed* in the browser, never
  *initiated*.
- **F-4 · MAJOR.** `/judge` blank state's remedy is
  "run `python3 bin/judgement.py init` from the project root". The
  fabric DB should be auto-initialized (or one-click) — a brand-new user
  hits a CLI wall on the "Review" nav tab within 30 seconds.
- **F-5 · MINOR.** Two parallel product spines are visible from the nav
  (domain tournaments vs. landscape/runs/catalog) with no explanation of
  how they relate; `/start` onboards only the tournament spine and never
  mentions Catalog/Runs, which sit in the same nav bar.
- **F-6 · MAJOR.** `/prompts` model dropdowns (Judge / Reflection /
  Curator) are flooded with obviously wrong choices: whisper STT
  variants, kokoro/qwen3 TTS voices, embedding models — 25+ entries of
  which maybe 2 are chat models. The backend model list is passed
  through unfiltered; a user could select `whisper-large-v3` as their
  Judge model.
- **F-7 · MINOR.** `/new` server-side file browser defaults to
  `/Users/user/projects` — a hardcoded path that doesn't exist on this
  machine (user is not `admin`), so the picker starts on a dead
  directory.
- **F-8 · POLISH.** Nav label inconsistency: nav says "Review" but page
  is "Review queue" (route `/judge`); "Results" page is served by
  `JudgementsLive` and `/judgements` is an unlisted alias.

## 2. Seeded pass

Seed script (from repo root, python3): `bin.catalog` init +
`create_project("unity-explorer")` + `create_source(...)`;
`bin.workflow_runs` start + record_stage + set_status
`awaiting-approval`; `bin.campaigns` create_campaign + create_finding.

### /catalog project detail

After seeding, `/catalog` shows a project card (name, status pill,
description, updated date, "0 components · 2 sources · 0 snapshots") and
`/catalog/unity-explorer` renders Components / Sources / Snapshots
sections. Sources show name, kind, trust badge (`TIER1 · system`,
`TIER2 · internal`) and locator.

- **F-9 · MAJOR (charter step 2).** Source rows show only kind, trust
  tier, and locator. **No health, no last-sync timestamp, no credential
  status, no verify/sync action** — step 2 ("verify sources: health,
  last sync, trust tier, credential status") is ¼ implemented (trust
  tier only), and it's display-only: nothing on the page can be clicked
  to test a connection.
- **F-10 · MINOR.** Catalog detail is entirely read-only: no add
  component/source, no edit, no snapshot capture. Consistent with F-1 —
  the whole landscape spine is a CLI mirror.

### /runs list + detail (awaiting-approval, DT_OPERATOR unset)

`/runs` lists the seeded run: status pill `awaiting-approval`, workflow
id, "3 stages · started …". Detail page
`/runs/release-unity-explorer-0001` shows run id, temporal run id, a
stage timeline (build ✓ done, canary ✓ done, approval pending, each
timestamped) and an Approval panel.

With `DT_OPERATOR` unset the Approve/Reject buttons render **`disabled`**
with the exact hint *"No operator identity — set `DT_OPERATOR` to enable
approval."* — verified in the HTML
(`<button … value="approve" … disabled id="approve-button">`). This is
the correct fail-closed behavior. 👍

- **F-11 · MAJOR (charter step 8).** "Authenticated principal" is a
  **server-side environment variable** (`System.get_env("DT_OPERATOR")`,
  `runs_live.ex:73`), not a browser identity. Anyone who can reach the
  UI approves as whoever the server was booted as. Fail-closed when
  unset is right, but the charter's step 8 (authenticated principal +
  immutable audit tied to a person) needs real per-user auth
  (login/session, at minimum an operator prompt with an audit note),
  not a boot-time env var shared by every browser tab.
- **F-12 · MINOR.** The run detail shows no link back to what is being
  approved: no spec digest surfaced (seed had none), no patch/test
  evidence panel, no diff. An approver must take the decision on faith
  — charter step 9's "inspect patch + test evidence" surface doesn't
  exist here yet.
- **F-13 · POLISH.** Temporal run id is truncated in display
  ("run-aaaa-bbb" shown for `run-aaaa-bbbb`) with no copy affordance.

### /judge (seeded)

Once the judgement DB exists, `/judge` shows "Review queue · 0 pending ·
0 ratings recorded" and "Inbox zero" with next-step hints ("Generate
pairs from a domain, or check the results page"). Good empty state; the
earlier "fabric DB missing" banner is gone. No new findings beyond F-4.

### Campaigns: in the DB but no UI (the headline gap)

The seed created `campaign(bugsweep-aug, kind=bugsweep, objective, time
window, base_commit)` and `finding(npe-scene-loader, sentry, root
cause, dedup notes)` — verified present in the very same
`/tmp/dt-journey2/judgements.db` the UI reads (tables `campaign`,
`finding`, `finding_evidence`, plus `lens_verdict` / `validation_ledger`
indexes).

- **F-14 · BLOCKER (charter steps 3–5).** `grep -ric campaign ui/lib/`
  → **zero matches**. The backend schema for campaigns, findings,
  finding evidence, lens verdicts, and a validation ledger exists and
  accepts writes, but not a single LiveView, route, nav entry, or even
  read-only table renders any of it. Charter steps 3 (start campaign),
  4 (collect signals), 5 (triage findings/dossiers) are 0% present in
  the browser even though the data layer is live underneath. A user who
  ran `bin/campaigns.py` from the CLI would have no way to ever see the
  results.

## 3. Resilience spot-checks

| Probe | Expected | Actual | Severity |
|---|---|---|---|
| Unknown route `/nope` | 404 page | **HTTP 404** (no crash, no 500) | ✅ OK |
| Bad run id `/runs/not-a-run` | graceful | **HTTP 200**, page renders "No run recorded for not-a-run" + "Back to runs" link | ✅ graceful (POLISH: arguably should be 404 status for correctness) |
| Bad project `/catalog/unity-explorer` pre-seed | graceful | HTTP 200, "No project named unity-explorer in the catalog" + back link | ✅ graceful |
| Bad domain param `/judge?domain=x` | graceful | HTTP 200, "Filtered to x · clear" chip + Inbox zero (pre-init it additionally showed the fabric-DB banner instead of erroring) | ✅ graceful |

- **F-15 · POLISH.** Entity-not-found pages (`/runs/:id`,
  `/catalog/:project`) return HTTP 200 with an inline "not found" body.
  Fine for humans, wrong for tooling/monitoring — a dead link never
  registers as an error. No crashes observed anywhere; resilience is a
  genuine strength of this build.

## 4. Scored journey table (charter steps 1–11)

Score: ✅ works-in-UI · 🟡 partial-in-UI · ⌨️ CLI-only · ❌ missing entirely

| # | Charter step | Score | Evidence |
|---|---|---|---|
| 1 | Create Landscape from UI, no CLI seeding | ⌨️ CLI-only | `/catalog` empty state literally instructs `python3 bin/catalog.py create-project` (F-1); UI is a read-only mirror |
| 2 | Verify sources: health, sync, trust, creds | 🟡 partial | `/catalog/:project` shows kind + trust tier + locator only; no health/last-sync/credential status, no verify action (F-9) |
| 3 | Start Campaign (objective + window) | ⌨️ CLI-only / ❌ UI | `bin/campaigns.py create_campaign` works and persists; zero campaign UI, `/campaigns` 404, 0 grep hits in `ui/lib` (F-2, F-14) |
| 4 | Collect signals: per-source counts | ❌ missing | No signal-collection surface anywhere in the UI; findings written to DB are invisible (F-14) |
| 5 | Triage findings: deduped dossiers | ❌ missing | `finding` + `finding_evidence` tables render nowhere (F-14) |
| 6 | Generate cited WorkOrders | ⌨️/🟡 | Domain wizard (`/domains/new`) can generate candidates for tournaments, but no WorkOrder/cited_evidence surface was reachable in this walkthrough; plan itself says generation never stamps cited_evidence |
| 7 | Judge: review bar, pairwise, rationale | 🟡 partial | `/judge` review queue UI exists and is polished, but blank until a CLI (`bin/judgement.py init`) or a full domain generation run feeds it (F-4) |
| 8 | Approve: authenticated, fail-closed, audit | 🟡 partial | Fail-closed disabled buttons + hint verified in HTML; but "identity" = server env var `DT_OPERATOR`, not an authenticated browser principal (F-11) |
| 9 | Execute: sandbox, patch + test evidence | ⌨️ CLI-only | Runs are observable (`/runs` timeline) but only startable via `python3 -m bin.release_workflow.client`; no patch/test evidence panel on the run page (F-3, F-12) |
| 10 | Ship: PR, CI, canary, promote/rollback | ❌ missing | Stage names (build/canary) display in the timeline, but no PR link, CI state, promote/rollback controls; plan admits these stages are stubs |
| 11 | Learn: outcomes → rules w/ provenance | ❌ missing | No rules/proposals/promotion surface in any route; no nav entry |

**Score summary: 0 of 11 steps fully work in the UI.** 4 partial (2, 7,
8, and charitably 6), 3 CLI-only (1, 3, 9), 4 missing entirely
(4, 5, 10, 11). The app today is an excellent *tournament/judging* tool
with a read-only window onto a release pipeline — the campaign middle of
the charter journey (3→6) doesn't exist in the browser at all.

## 5. Ranked top-10 UX gaps

1. **BLOCKER — No campaign UI whatsoever** (F-2/F-14): schema live, data
   writable, zero rendering. Steps 3–5 are invisible.
2. **BLOCKER — Landscape creation is CLI-only** (F-1): journey step 1
   dies on the spot; the empty state advertises the CLI.
3. **MAJOR — Runs cannot be started from the browser** (F-3): the UI is
   a projection, not a control surface, for steps 9–10.
4. **MAJOR — Approval identity is a boot-time env var** (F-11): fail-
   closed is right, but there's no per-user principal, so audit
   attribution is theater in multi-viewer setups.
5. **MAJOR — Source verification absent** (F-9): trust tier is the only
   one of four promised source attributes shown; nothing is actionable.
6. **MAJOR — Judge tab dead-ends new users into `bin/judgement.py
   init`** (F-4): the DB the page needs should be created by the app.
7. **MAJOR — Prompt studio model dropdowns list whisper/TTS/embedding
   models as Judge candidates** (F-6): unfiltered backend passthrough
   invites nonsense configs.
8. **MINOR — Two unexplained product spines in one nav** (F-5):
   tournament flow (Start→Domains→Review→Results) vs. landscape flow
   (Catalog/Runs) with no connective tissue or shared onboarding.
9. **MINOR — Approval page shows no evidence of what's being approved**
   (F-12): no spec digest, patch, or test results next to the buttons.
10. **MINOR — Hardcoded `/Users/user/projects` default in the direct-
    bracket file browser** (F-7) plus not-found pages returning 200
    (F-15) — small trust erosions.

## 6. Three quick wins

1. **Read-only campaigns page (~1 LiveView).** `CampaignsLive` listing
   `campaign` + `finding` rows from the DB the UI already opens, linked
   from `/catalog/:project`. Turns steps 3–5 from "invisible" to
   "observable" in an afternoon, mirroring exactly how `/runs` already
   mirrors `workflow_run`.
2. **Auto-init the judgement DB** on first LiveView mount (or a
   one-click "Initialize" button in the `/judge` banner) — deletes the
   worst first-session CLI wall (F-4) with a one-line call to the same
   init the CLI runs.
3. **Filter the prompt-studio model dropdowns** to chat-capable models
   (or at least sort LLMs first with a "show all" toggle) — F-6 is pure
   list hygiene and immediately makes `/prompts` look intentional.

## Verdict (TL;DR)

The app never crashed once across 13 routes, blank or seeded, with
hostile params — resilience and empty-state *tone* are strong. But
measured against its own charter ("acceptance = this works in a
browser"), **0/11 journey steps are fully browser-complete**: the
tournament spine (start → domain wizard → judge → results) is real UI,
while the shipping spine is a read-only projection of CLI actions and
the campaign layer (steps 3–5) has schema and data but literally zero
pixels. Top blockers: no campaign UI (F-14), CLI-only landscape
creation (F-1); top structural risk: env-var approval identity (F-11).


## Appendix: environment notes

- Server: `PORT=4020 PROMPT_BACKEND=local DATA_TOURNAMENTS_HOME=/tmp/dt-journey2 mix phx.server`
- Server disposition: killed at end of review (reviewer's throwaway
  instance on port 4020; seeded state remains in /tmp/dt-journey2 for
  reproduction).
- Seed used: `bin.catalog` init/create_project("unity-explorer")/2×
  create_source (github tier1, sentry tier2); `bin.workflow_runs`
  start → build/canary done, approval pending → status
  `awaiting-approval`; `bin.campaigns` create_campaign("bugsweep-aug",
  bugsweep) + create_finding("npe-scene-loader").
- One planned probe (`/judge?domain=<script>` XSS-shaped param) was not
  executed (command denied in the review environment); no XSS claim is
  made either way.
