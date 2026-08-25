# ADR 0001 — Persistence architecture for the project-landscape catalog

- Status: Proposed
- Date: 2026-08-17
- Deciders: 2-person team (data-tournaments)
- Relates to: `docs/plans/unity-explorer-release-platform.md` Phase 0a (lines 29–32),
  Phase 1 (lines 41–44); `docs/research/context-platform-survey-2026.md`;
  `docs/research/durable-workflow-orchestration-2026.md`
- Companion: ADR 0002 (content-addressed artifact storage)

## Context

We are adding a project-landscape catalog (`ProjectCatalog → LandscapeSnapshot →
ContextPack → WorkOrder/WorkflowSpec → Temporal WorkflowRun → sandbox execution`,
per `docs/plans/unity-explorer-release-platform.md` lines 19–24). Phase 1 needs
persistence for: **Project, Component, Source, Capability, Skill, Environment,
Policy, EvidenceRef, LandscapeSnapshot, ContextPack, WorkflowSpec, WorkflowRun**.

This ADR decides *where those rows live and who owns the schema*. Every claim in
the inventory below was verified against the file/line cited at commit `29ff4ef`.

## 1. Inventory — current storage topology (verified)

### 1.1 Schema ownership: Python

- The single fabric DB lives at `$DATA_TOURNAMENTS_HOME/judgements.db`
  (`bin/judgement.py:64–65`: `DATA_HOME = Path(os.environ.get("DATA_TOURNAMENTS_HOME",
  "/tmp/data-tournaments"))`, `DB_PATH = DATA_HOME / "judgements.db"`).
- The DDL source of truth is `bin/judgement_schema.sql`, applied by
  `bin/judgement.py init_db()` (`bin/judgement.py:178–192`): it `mkdir -p`s the
  home, runs `conn.executescript(schema_sql)`, then calls
  `_migrate_pending_judgement(conn)` and `bin.optimizer_runs.init()`.
- Migration style today is **idempotent bootstrap**: every table in the schema
  file is `CREATE TABLE IF NOT EXISTS` (`bin/judgement_schema.sql:20,39,60,85,114`)
  and post-v0 columns are added by a `PRAGMA table_info` check + `ALTER TABLE`
  (`bin/judgement.py:289–296`, adding `pending_judgement.domain_id`). There is no
  migration ledger/version table.
- `bin/judgement.py:19–20` states the intent explicitly: "The schema lives at
  bin/judgement_schema.sql so Python and Elixir can both reference it as the
  single source of truth."
- The Elixir side explicitly refuses to bootstrap: "The Python side is the source
  of truth for schema bootstrap; if the fabric DB doesn't exist when the UI
  starts, we don't try to create it here — surfaces a banner instead."
  (`ui/lib/tournament_ui/judgement.ex:14–18`).
- A second Python module owns one more table in the *same* DB:
  `bin/optimizer_runs.py:32–48` defines `optimizer_run` (`CREATE TABLE IF NOT
  EXISTS`), `_db_path()` points at the same `judgements.db`
  (`bin/optimizer_runs.py:51–53`), and `init()` is idempotent
  (`bin/optimizer_runs.py:62–65`).
- `bin/domains.py:15–24` opens the same DB (`_db_path()`, `_connect()` with
  `PRAGMA foreign_keys = ON`) for domain CRUD.

### 1.2 Tables that exist today (`bin/judgement_schema.sql` + `bin/optimizer_runs.py`)

| Table | Where defined | Notes |
|---|---|---|
| `eval_template` | `judgement_schema.sql:20–34` | versioned rubrics; JSON `output_definition`; `UNIQUE(name, version)` |
| `job_configuration` | `judgement_schema.sql:39–53` | rubric×rater binding; JSON `rater_config` |
| `pending_judgement` | `judgement_schema.sql:60–80` | queue rows; JSON `trace_payload`; `domain_id` added by runtime migration (`judgement.py:289–296`) |
| `score` | `judgement_schema.sql:85–109` | immutable output, two rows per judgement paired by `rating_id`; JSON `metadata` |
| `domain` | `judgement_schema.sql:114–125` | named tournaments; JSON `corpus_source` |
| `optimizer_run` | `optimizer_runs.py:32–48` | run status + appended `log` text + JSON `result` |

Global pragmas in the schema file: `PRAGMA foreign_keys = ON; PRAGMA
journal_mode = WAL;` (`judgement_schema.sql:16–17`).

Separate from the fabric DB, each tournament run produces its own SQLite file
(one DB = one tournament) which the UI reads read-only
(`ui/lib/tournament_ui/tournament.ex:1–8`, scanning `/tmp/*.db`). Those are
out of scope here but establish the precedent of *many small SQLite files as
run artifacts* alongside *one shared fabric DB*.

### 1.3 How ui/ accesses SQLite: exqlite directly, no Ecto

- `ui/mix.exs:62` — `{:exqlite, "~> 0.23"}`. The full deps list
  (`ui/mix.exs:41–65`) contains **no** `ecto`, `ecto_sql`, or `ecto_sqlite3`.
- Access-layer modules under `ui/lib/tournament_ui/` that call
  `Exqlite.Sqlite3` (verified by grep for `Exqlite.Sqlite3`):
  - `judgement.ex` — read/write adapter over the fabric DB. Reads open
    `mode: :readonly` (`judgement.ex:568`); writes go through a hand-rolled
    `write_transaction/1` doing `BEGIN`/`COMMIT`/`ROLLBACK`
    (`judgement.ex:600–622`). `submit_human/4` (`judgement.ex:140+`) INSERTs the
    two `score` rows and completes the pending row — so **Elixir already
    writes** to the fabric DB today.
  - `optimizer_runs.ex` — mostly-read adapter, but `start/1` INSERTs an
    `optimizer_run` row (`optimizer_runs.ex:24–40`); the Python runner then
    updates it (moduledoc, `optimizer_runs.ex:2–9`).
  - `domains.ex` — read-only adapter; moduledoc: "Writes (create/archive) go
    through Python (`bin/domains.py`) so the Langfuse Prompts side and the
    SQLite side stay atomic" (`domains.ex:2–7`).
  - `inspect.ex` — read-only adapter for /inspect (`inspect.ex:1–18`).
  - `tournament.ex` — read-only adapter over per-run tournament DBs.
  - `input_sources.ex` — materialises corpus rows from arbitrary user-supplied
    DBs (sqlite/postgres URLs) into files (`input_sources.ex:1–25`).
- Note (pre-existing risk, not introduced here): none of the Elixir writers set
  `busy_timeout` (grep for `busy_timeout` over `ui/lib` and `bin` returns
  nothing), so a concurrent Python write transaction can surface as
  `SQLITE_BUSY` in the UI. WAL mode mitigates but does not eliminate this.

### 1.4 How DATA_TOURNAMENTS_HOME flows

- Python: read per-module with an identical default —
  `bin/judgement.py:64`, `bin/domains.py:16`, `bin/optimizer_runs.py:52`,
  `bin/feedback.py:111`, `bin/generate_cards.py:39`, `bin/optimize.py:58`,
  `bin/prompts.py:43`; tests point it at a tmpdir (`tests/conftest.py:34`).
- Elixir: `TournamentUi.Paths.home/0`
  (`ui/lib/tournament_ui/paths.ex:11`) plus the same inline pattern in
  `judgement.ex:26`, `domains.ex:116`, `optimizer_runs.ex:14`, `inspect.ex:16`,
  `langfuse_prompts.ex:178`.
- Shell: `bin/_env.sh:4` (`DATA_HOME="${DATA_TOURNAMENTS_HOME:-/tmp/data-tournaments}"`).
- Elixir tests drive the Python bootstrap through the env var, e.g.
  `ui/test/tournament_ui/judgement_test.exs:9,20,42` sets
  `DATA_TOURNAMENTS_HOME` and shells out to Python to init the DB.

**Summary of the existing pattern:** one shared SQLite file, WAL mode, Python
owns DDL and idempotent bootstrap, both runtimes read, both runtimes perform
narrow well-defined writes, `DATA_TOURNAMENTS_HOME` is the only coordination
mechanism. Every fabric feature to date (domains, judgements, optimizer runs)
followed this pattern.

## 2. Decision

**Option (a): the catalog lives in the same `judgements.db`, with Python
remaining the sole schema owner.** New tables are added to
`bin/judgement_schema.sql` (mutable catalog) and applied by the existing
idempotent `init_db()` path. Elixir gets a new read-mostly adapter module
(`TournamentUi.Catalog`, same shape as `domains.ex`). Immutable
content-addressed artifacts (snapshots, packs) get insert-only tables whose
primary key is the digest; large payloads live in a filesystem CAS under
`$DATA_TOURNAMENTS_HOME/cas/` — see ADR 0002.

Write-ownership rules (extending, not replacing, today's discipline):

| Entity group | Schema owner | Writers | Elixir role |
|---|---|---|---|
| Project, Component, Source, Capability, Skill, Environment, Policy | Python (`judgement_schema.sql`) | Python CLI/modules (`bin/catalog.py`, future) | read + narrow UI edits routed through Python, exactly like `domains.ex` does today |
| EvidenceRef, LandscapeSnapshot, ContextPack, WorkflowSpec | Python | Python only (insert-only; produced by snapshot/pack builders from Phase 0b contracts) | read-only |
| WorkflowRun | Python | Python Temporal Activities only (workflows may not touch the DB directly — plan invariant, `unity-explorer-release-platform.md:71`) | read-only projection for the audit UI |

### Why (a)

1. **It is the only option that matches every existing pattern.** Three
   features (domains, judgement fabric, optimizer runs) already share this DB
   with this ownership split; the codebase self-documents the contract in both
   runtimes (`judgement.py:19–20`, `judgement.ex:14–18`). Adding a fourth
   feature the same way is near-zero architectural risk.
2. **Both runtimes need catalog access, and both already have it.** The
   Phoenix UI reads via exqlite adapters; Python (generators, judges,
   Temporal workers) reads/writes via `sqlite3`. No new driver, dependency, or
   service on either side.
3. **Temporal fits without ceremony.** Workers are Python (no Elixir SDK —
   `docs/research/durable-workflow-orchestration-2026.md:24`), and the research
   doc already anticipates "Phoenix reads workflow state that Python workers
   project into" a store (`durable-workflow-orchestration-2026.md:25`).
   A `workflow_run` projection table written by Activities in the fabric DB is
   the smallest implementation of that, and it lands next to the `work_order` /
   judgement rows it must join against (WorkOrders are judged through the
   existing tournament machinery — plan lines 20, 50).
4. **Cross-entity joins stay trivial.** Judging a WorkflowSpec reuses
   `pending_judgement`/`score`; a ContextPack cites EvidenceRefs which cite
   Sources which belong to Projects. One DB means these are plain FK joins, the
   same way `pending_judgement.domain_id → domain.id` works today.
5. **Right-sized for a 2-person team.** No new infra, no ORM adoption, no
   dual-migration story. The failure modes (SQLITE_BUSY under concurrent
   writes) are already present and are addressed by hygiene (below), not by a
   new engine.

### Concurrency hygiene shipped with this decision

- Every writer (both runtimes) sets `PRAGMA busy_timeout = 5000` (or exqlite
  equivalent) on connection open — closes the pre-existing gap noted in §1.3.
- Keep the single-*schema*-owner rule strict: Elixir never executes DDL.
- Keep writes short-lived and per-connection, as `write_transaction/1` already
  does (`judgement.ex:600–622`).
- Insert-only tables (digest-keyed) are enforced by convention plus a
  `CREATE TRIGGER ... BEFORE UPDATE ... RAISE(ABORT)` guard (cheap in SQLite,
  see §3).

### Explicitly considered and rejected

**(b) Separate `catalog.sqlite`.** Rejected. It buys write isolation we don't
need (catalog write volume is tiny and human-paced) at the cost of losing FK
joins between catalog entities and the judgement fabric — which Phase 2 needs
("WorkOrders cite EvidenceRef IDs; judge view shows cited refs", plan line 50),
and which would otherwise require `ATTACH DATABASE` in every reader in both
runtimes, or application-level joins. It also doubles the bootstrap story
(two init paths, two "DB missing" banners in the UI). If pack/snapshot *bulk*
ever threatens the fabric DB's size, the escape valve is ADR 0002's filesystem
CAS, not a second relational DB.

**(c) Adopt Ecto + SQLite in Elixir.** Rejected for now. The research doc's
"~5 Ecto schemas" phrasing (`context-platform-survey-2026.md:154–156, 180–182`)
was a conceptual sketch written before auditing the repo; the reality is that
ui/ uses exqlite directly (`ui/mix.exs:62`) and Ecto is not a dependency.
Adopting it would mean either (i) Ecto migrations become the schema owner —
flipping ownership to the runtime that *doesn't* run the generators, judges,
optimizer, or Temporal workers, and breaking the "pure-UI installs call
`python3 bin/judgement.py init` once" bootstrap (`judgement.ex:16–18`); or
(ii) Ecto schemas as a read-mapping layer over Python-owned DDL — which adds
`ecto`, `ecto_sql`, `ecto_sqlite3` and a Repo pool to save us hand-written
row-decoding we have already written six times and that works. Changesets and
query composition are real benefits, but the UI's catalog surface in Phase 1 is
lists + detail views + a few Python-routed edits; that does not pay for an ORM
adoption. **Revisit trigger:** if the Elixir side ever becomes a first-class
*writer* of complex catalog mutations (nested forms, validations), reopen this
as its own ADR.

**(d) Postgres.** Rejected for Phase 1. It solves concurrency and typed JSON
(`jsonb`) we don't yet need, and costs a running service, credentials plumbing
through `DATA_TOURNAMENTS_HOME`-style config, a Python driver, an Elixir driver
(and realistically Ecto), and a dev/test bootstrap rewrite (today tests get a
fresh store by pointing an env var at a tmpdir — `tests/conftest.py:34`,
`judgement_test.exs:9`). Note Temporal *server* self-hosting brings its own
Postgres for its internal state (`durable-workflow-orchestration-2026.md:19`),
but that instance is Temporal's implementation detail; piggybacking our domain
schema on it would couple our catalog to workflow-engine ops. **Revisit
triggers:** multi-host writers (UI and workers on different machines), row
volumes where SQLite write serialization measurably hurts, or a need for
concurrent long transactions.

## 3. Schema sketch (DDL sketch, not final)

Conventions follow the existing schema: `INTEGER PRIMARY KEY AUTOINCREMENT`
for mutable entities, `TEXT` ISO-8601 timestamps via `datetime('now')`, JSON in
`TEXT` columns. Digest-keyed tables use `TEXT PRIMARY KEY` (`sha256:<hex>`).

JSON-column policy (derived from what already works — cf. the deliberate
promotion of `ratingId` out of JSON into a real column,
`judgement_schema.sql:12–14`):
- **Acceptable:** payloads read whole and never filtered on — display blobs,
  provenance detail, adapter-specific config (like `rater_config`,
  `corpus_source` today).
- **Harmful (promote to columns):** anything queried, joined, or indexed —
  ids, digests, statuses, timestamps, foreign keys, names/versions.

```sql
-- ── Mutable catalog ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS project (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,          -- e.g. 'unity-explorer'
  description  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'active',-- 'active'|'archived'
  metadata     TEXT NOT NULL DEFAULT '{}',    -- JSON: display-only extras
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS component (        -- deployable/buildable unit
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,                 -- 'app'|'plugin'|'service'|'library'|...
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS source (           -- where evidence comes from
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,                 -- 'git'|'github-issues'|'github-releases'
                                              -- |'unity-cloud-build'|'docs'|'api'|'mcp'
  locator      TEXT NOT NULL,                 -- URL / path / repo (queried: real column)
  trust_tier   INTEGER NOT NULL DEFAULT 3,    -- 1=first-party..3=external (plan line 73)
  config       TEXT NOT NULL DEFAULT '{}',    -- JSON: adapter-specific (like rater_config)
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS capability (       -- what an agent may do, e.g. 'judge','build'
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  description  TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skill (            -- versioned procedure (SKILL.md folder)
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  version      INTEGER NOT NULL,              -- mirrors eval_template(name,version)
  locator      TEXT NOT NULL,                 -- path/URL of the SKILL.md folder
  digest       TEXT,                          -- content digest of the skill folder, if pinned
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS environment (      -- where work may run
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,          -- 'e2b-preflight'|'microvm-linux'|...
  kind         TEXT NOT NULL,                 -- 'sandbox'|'ci'|'control-plane'
  config       TEXT NOT NULL DEFAULT '{}',    -- JSON: image, flake ref, egress policy ref
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS policy (           -- approval / gating rules
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL,                 -- 'approval'|'egress'|'secret-scope'
  rule         TEXT NOT NULL,                 -- JSON body; evaluated in code, never SQL
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Relations (plain join tables; add columns only when a relation needs data)
CREATE TABLE IF NOT EXISTS component_capability (
  component_id  INTEGER NOT NULL REFERENCES component(id),
  capability_id INTEGER NOT NULL REFERENCES capability(id),
  PRIMARY KEY (component_id, capability_id)
);
CREATE TABLE IF NOT EXISTS project_skill (
  project_id INTEGER NOT NULL REFERENCES project(id),
  skill_id   INTEGER NOT NULL REFERENCES skill(id),
  PRIMARY KEY (project_id, skill_id)
);

-- ── Immutable, content-addressed (insert-only; see ADR 0002) ──────────
CREATE TABLE IF NOT EXISTS evidence_ref (
  digest       TEXT PRIMARY KEY,              -- 'sha256:<hex>' of canonical serialization
  source_id    INTEGER NOT NULL REFERENCES source(id),
  kind         TEXT NOT NULL,                 -- 'commit'|'issue'|'release'|'build'|'file'|...
  locator      TEXT NOT NULL,                 -- stable pointer (URL, path@commit)
  trust_tier   INTEGER NOT NULL,              -- copied at capture time (tiers can't drift)
  summary      TEXT NOT NULL DEFAULT '',
  body         TEXT,                          -- inline if small; NULL → CAS file (ADR 0002)
  captured_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS landscape_snapshot (
  digest       TEXT PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  schema_version INTEGER NOT NULL,
  manifest     TEXT NOT NULL,                 -- JSON: canonical serialized snapshot
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS snapshot_evidence (   -- which refs a snapshot includes
  snapshot_digest TEXT NOT NULL REFERENCES landscape_snapshot(digest),
  evidence_digest TEXT NOT NULL REFERENCES evidence_ref(digest),
  PRIMARY KEY (snapshot_digest, evidence_digest)
);

CREATE TABLE IF NOT EXISTS context_pack (
  digest          TEXT PRIMARY KEY,
  snapshot_digest TEXT NOT NULL REFERENCES landscape_snapshot(digest),
  role            TEXT NOT NULL,              -- 'creator'|'judge'|'executor' (plan line 34)
  schema_version  INTEGER NOT NULL,
  manifest        TEXT NOT NULL,              -- JSON canonical body (or CAS pointer)
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS workflow_spec (    -- typed, judgeable (plan line 20)
  digest          TEXT PRIMARY KEY,
  project_id      INTEGER NOT NULL REFERENCES project(id),
  name            TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  spec            TEXT NOT NULL,              -- JSON canonical body
  pack_digest     TEXT REFERENCES context_pack(digest),
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Immutability guard, one per digest-keyed table:
CREATE TRIGGER IF NOT EXISTS evidence_ref_immutable
  BEFORE UPDATE ON evidence_ref
  BEGIN SELECT RAISE(ABORT, 'evidence_ref rows are immutable'); END;
-- (same trigger pattern for landscape_snapshot / context_pack / workflow_spec)

-- ── Mutable projection of Temporal state (written by Activities only) ──
CREATE TABLE IF NOT EXISTS workflow_run (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_digest        TEXT NOT NULL REFERENCES workflow_spec(digest),
  temporal_workflow_id TEXT NOT NULL,         -- keyed by work_order_id (plan line 54)
  temporal_run_id    TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'running',
                     -- 'running'|'awaiting-approval'|'done'|'failed'|'canceled'
  environment_id     INTEGER REFERENCES environment(id),
  detail             TEXT NOT NULL DEFAULT '{}',  -- JSON: last activity, error, timings
  started_at         TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at        TEXT,
  UNIQUE(temporal_workflow_id, temporal_run_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_run_status ON workflow_run(status);
```

Notes:
- **Temporal remains the source of truth for execution state**; `workflow_run`
  is a queryable projection for the LiveView audit UI (plan line 56), matching
  the `optimizer_run` precedent (Python writes progress, Elixir reads —
  `optimizer_runs.ex:2–9`).
- Provenance fields on mutable rows are system-stamped, never model-supplied,
  following the `bin/workorder.py` rule (its module docstring: "The SYSTEM
  supplies provenance … stamps these").
- Immutable rows coexist with mutable rows cleanly because they *reference*
  mutable rows only by integer FK captured at insert time, plus copied
  attributes that must not drift (e.g. `evidence_ref.trust_tier`).

## 4. Migration plan (each step independently shippable)

Today's baseline: `init_db()` runs `executescript` on the schema file plus
targeted `ALTER TABLE`s (`judgement.py:178–192, 289–296`). All steps below ride
that exact mechanism; `CREATE TABLE IF NOT EXISTS` keeps every step idempotent
and safe to re-run.

1. **Concurrency hygiene (no schema change).** Add `busy_timeout` to
   `_connect()` in `bin/judgement.py` / `bin/domains.py` / `bin/optimizer_runs.py`
   and to the exqlite `open` sites in `judgement.ex` / `optimizer_runs.ex`.
   *Rollback:* revert; behavior returns to today's.
2. **Mutable catalog tables.** Append `project`, `component`, `source`,
   `capability`, `skill`, `environment`, `policy` + join tables to
   `bin/judgement_schema.sql`. No code reads them yet. Existing DBs pick the
   tables up on the next `init` (idempotent). *Rollback:* remove the DDL;
   stray empty tables in existing DBs are inert and can be dropped manually or
   left (they are `IF NOT EXISTS`-guarded, so re-adding later is safe).
3. **Python catalog module + CLI (`bin/catalog.py`)** with CRUD mirroring
   `bin/domains.py`, plus pytest coverage against a tmp
   `DATA_TOURNAMENTS_HOME` (pattern: `tests/conftest.py:34`). *Rollback:*
   delete the module; tables remain but unused.
4. **Elixir read adapter + catalog UI.** `TournamentUi.Catalog` copying the
   `domains.ex` shape (read-only; edits shell out to `bin/catalog.py`, same
   atomicity argument as `domains.ex:2–7`). MCP Resources exposure (plan line
   44) reads through the same Python module. *Rollback:* remove
   module/LiveView; no data impact.
5. **Immutable artifact tables + CAS.** Add `evidence_ref`,
   `landscape_snapshot`, `snapshot_evidence`, `context_pack`, `workflow_spec`
   DDL + immutability triggers + the `$DATA_TOURNAMENTS_HOME/cas/` layout (ADR
   0002). Writers are the Phase-0b/Phase-2 Python builders; digests come from
   the Phase-0b canonical serialization. *Rollback:* stop writing; immutable
   rows are self-contained and can be dropped wholesale (nothing mutable
   depends on them until Phase 2 wires `work_order` citations).
6. **`workflow_run` projection** when the Temporal spike lands (Phase 3).
   Written only from Python Activities. *Rollback:* drop the table; Temporal
   retains full history, so the projection can be rebuilt by a backfill script
   querying the Temporal frontend.

Sequencing note: steps 2–4 are Phase 1; 5 is Phase 0b/2; 6 is Phase 3. Any
step can ship without the ones after it.

## 5. Consequences

Easier:
- Phase 1 starts immediately: no new deps in either runtime, no infra.
- One bootstrap (`judgement.py init`), one env var, one "DB missing" banner.
- FK joins across catalog ↔ judgement fabric ↔ optimizer ↔ workflow runs;
  the judge view's "shows cited refs" (plan line 50) is a two-join query.
- Tests keep the tmpdir-env-var pattern in both runtimes unchanged.

Harder / accepted costs:
- SQLite single-writer-at-a-time: acceptable at this write volume, but the
  busy_timeout hygiene in step 1 is mandatory, and bulk snapshot inserts
  should be batched in one short transaction.
- No migration ledger: additive `IF NOT EXISTS` + `PRAGMA table_info` ALTERs
  scale poorly past ~a dozen ad-hoc migrations. If migration count grows, add
  a tiny `schema_migration(version)` table — Python-owned, still no framework.
- Elixir stays a second-class writer (by design). Complex catalog editing UX
  will route through Python subprocess calls like domain creation does today;
  if that becomes the bottleneck, that is the trigger to reopen option (c).
- The fabric DB file gains tables from multiple concerns. Mitigated by ADR
  0002 keeping bulk out of the DB, and by the fact that this is already true
  today (judgements + domains + optimizer runs).

Rejected options are documented with revisit triggers in §2.
