-- judgement_schema.sql — judgement-fabric v0
--
-- One central SQLite DB shared across all tournaments. Path comes from
-- $DATA_TOURNAMENTS_HOME/judgements.db (default /tmp/data-tournaments/judgements.db).
--
-- The shape mirrors a subset of Langfuse's evaluator pipeline:
--   eval_template       ←→ Langfuse EvalTemplate
--   job_configuration   ←→ Langfuse JobConfiguration
--   pending_judgement   ←→ JobExecution rows where status=PENDING
--   score               ←→ Langfuse Score
--
-- Two Score rows per judgement, joined via metadata.ratingId (stored as
-- a column here for query convenience even though Langfuse keeps it in
-- the JSON metadata blob).

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ── Rubrics (versioned) ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS eval_template (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT    NOT NULL,
  version         INTEGER NOT NULL,
  -- JSON: {verdict_enum: [...], confidence_enum: [...], rationale_required: bool,
  --        description: "<short>"}.
  -- The judge's system prompt (what used to be `instructions` here) now lives
  -- in Langfuse Prompts; `langfuse_prompt_name` below points to it.
  output_definition TEXT  NOT NULL,
  langfuse_prompt_name TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  is_draft        INTEGER NOT NULL DEFAULT 0,
  UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_eval_template_name ON eval_template(name);

-- ── JobConfigurations ──────────────────────────────────────────────────
-- A binding of (rubric, scope, rater type, rater identity) → an active
-- evaluator. v0 keeps this minimal: one config per (template, rater_type).
CREATE TABLE IF NOT EXISTS job_configuration (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id     INTEGER NOT NULL REFERENCES eval_template(id),
  rater_type      TEXT    NOT NULL,  -- 'llm' | 'human' | 'agent' | 'programmatic'
  -- JSON for rater-specific config: e.g. for 'llm' → {"model","base_url","api_key_env"}
  rater_config    TEXT    NOT NULL DEFAULT '{}',
  -- Sampling rate 0..1; v0 always 1.0 (judge everything)
  sampling        REAL    NOT NULL DEFAULT 1.0,
  status          TEXT    NOT NULL DEFAULT 'active', -- 'active' | 'blocked' | 'paused'
  blocked_at      TEXT,
  block_reason    TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_config_active
  ON job_configuration(status, rater_type);

-- ── Pending queue ──────────────────────────────────────────────────────
-- One row per (trace, config) pair waiting to be judged. LLM-judge rows
-- get processed by a Python worker; human rows stay PENDING until the UI
-- submit happens. On submit/complete, the row is updated to status='done'
-- with a non-null `rating_id` pointing into the score table.
CREATE TABLE IF NOT EXISTS pending_judgement (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  config_id       INTEGER NOT NULL REFERENCES job_configuration(id),
  -- Trace identifiers. tournament_db_path + match_id is the v0 trace
  -- locator; trace_id is the optional Langfuse 32-hex trace id.
  tournament_db_path TEXT NOT NULL,
  match_id        INTEGER NOT NULL,
  trace_id        TEXT,
  -- Display payload — what the rater sees. JSON: {"label": "R3-2",
  -- "synthesis": "...", "winner_id": 1, "files": [...]}.
  trace_payload   TEXT    NOT NULL,
  status          TEXT    NOT NULL DEFAULT 'pending',  -- 'pending'|'done'|'error'|'cancelled'
  rating_id       TEXT,                                 -- uuid linking score rows
  error_message   TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status
  ON pending_judgement(status, config_id);
CREATE INDEX IF NOT EXISTS idx_pending_trace
  ON pending_judgement(tournament_db_path, match_id);

-- ── Score rows (the immutable output) ──────────────────────────────────
-- Two rows per judgement, joined via rating_id. This duplicates rater +
-- rubric_version on both rows for query simplicity; that's deliberate.
CREATE TABLE IF NOT EXISTS score (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  rating_id       TEXT    NOT NULL,           -- uuid; pairs up the 2 rows
  pending_id      INTEGER REFERENCES pending_judgement(id),
  template_id     INTEGER NOT NULL REFERENCES eval_template(id),
  rubric_version  INTEGER NOT NULL,
  -- 'judgement.verdict' or 'judgement.confidence'
  name            TEXT    NOT NULL,
  data_type       TEXT    NOT NULL,           -- 'CATEGORICAL' (v0 only uses categorical)
  value           TEXT    NOT NULL,
  -- JSON: {"rater": {"type":"human","userId":"..."}|{"type":"llm","model":"..."},
  --        "rationale": "..." (only on verdict row), ...}
  metadata        TEXT    NOT NULL DEFAULT '{}',
  -- Links back into the source-of-truth tournament DB so the comparison
  -- view can read trace inputs/outputs without duplicating them here.
  tournament_db_path TEXT NOT NULL,
  match_id        INTEGER NOT NULL,
  trace_id        TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_score_rating ON score(rating_id);
CREATE INDEX IF NOT EXISTS idx_score_name_value ON score(name, value);
CREATE INDEX IF NOT EXISTS idx_score_trace
  ON score(tournament_db_path, match_id);
CREATE INDEX IF NOT EXISTS idx_score_template ON score(template_id);

-- ── Domains (named card-prioritization tournaments) ─────────────────────
-- A domain bundles a corpus source + generator prompt + judge prompt.
-- See docs/plans/2026-05-10-domains.md for the design.
CREATE TABLE IF NOT EXISTS domain (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT    NOT NULL UNIQUE,
  description       TEXT    NOT NULL DEFAULT '',
  generator_prompt  TEXT    NOT NULL,
  judge_prompt      TEXT    NOT NULL,
  rubric            TEXT    NOT NULL DEFAULT 'card-prioritizer-v0',
  corpus_source     TEXT    NOT NULL,   -- JSON: {kind, ...}
  status            TEXT    NOT NULL DEFAULT 'active',
  created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_domain_status ON domain(status);

-- ═══════════════════════════════════════════════════════════════════════
-- Project-landscape catalog (ADR 0001 §3; ADR 0002 for CAS/immutability).
-- 'project' (landscape entity) is DISTINCT from 'domain' (evaluation
-- lens/corpus) above — do not conflate.
-- Python (bin/catalog.py) is the writer; Elixir reads only.
-- ═══════════════════════════════════════════════════════════════════════

-- ── Mutable catalog ─────────────────────────────────────────────────────
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
  status       TEXT NOT NULL DEFAULT 'active',
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_component_project ON component(project_id);

CREATE TABLE IF NOT EXISTS source (           -- where evidence comes from
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,                 -- 'git'|'github-issues'|'github-releases'
                                              -- |'unity-cloud-build'|'docs'|'api'|'mcp'
  locator      TEXT NOT NULL,                 -- URL / path / repo (queried: real column)
  trust_tier   INTEGER NOT NULL DEFAULT 3,    -- 1=first-party..3=external
  config       TEXT NOT NULL DEFAULT '{}',    -- JSON: adapter-specific (like rater_config)
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_source_project ON source(project_id);

CREATE TABLE IF NOT EXISTS capability (       -- what an agent may do, e.g. 'judge','build'
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  description  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skill (            -- versioned procedure (SKILL.md folder)
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  version      INTEGER NOT NULL,              -- mirrors eval_template(name,version)
  locator      TEXT NOT NULL,                 -- path/URL of the SKILL.md folder
  digest       TEXT,                          -- content digest of the skill folder, if pinned
  status       TEXT NOT NULL DEFAULT 'active',
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

-- ── Immutable, content-addressed (insert-only; see ADR 0002) ────────────
-- Digests are computed by bin/landscape (canonical.py) — never by the DB.
-- Bodies/manifests ≤64KiB live inline; larger ones live in the filesystem
-- CAS ($DATA_TOURNAMENTS_HOME/cas/sha256/<2hex>/<hex>) and the column is
-- NULL (ADR 0002 §2–3).
CREATE TABLE IF NOT EXISTS evidence_ref (
  digest       TEXT PRIMARY KEY,              -- hex sha256 of canonical serialization
  source_id    INTEGER NOT NULL REFERENCES source(id),
  kind         TEXT NOT NULL,                 -- 'commit'|'issue'|'release'|'build'|'file'|...
  locator      TEXT NOT NULL,                 -- stable pointer (URL, path@commit)
  trust_tier   INTEGER NOT NULL,              -- copied at capture time (tiers can't drift)
  summary      TEXT NOT NULL DEFAULT '',
  body         TEXT,                          -- inline if small; NULL → CAS file (ADR 0002)
  captured_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence_ref(source_id);

CREATE TABLE IF NOT EXISTS landscape_snapshot (
  digest       TEXT PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  schema_version INTEGER NOT NULL,
  manifest     TEXT,                          -- JSON canonical body; NULL → CAS file
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshot_project ON landscape_snapshot(project_id);

CREATE TABLE IF NOT EXISTS snapshot_evidence (   -- which refs a snapshot includes
  snapshot_digest TEXT NOT NULL REFERENCES landscape_snapshot(digest),
  evidence_digest TEXT NOT NULL REFERENCES evidence_ref(digest),
  PRIMARY KEY (snapshot_digest, evidence_digest)
);

CREATE TABLE IF NOT EXISTS context_pack (
  digest          TEXT PRIMARY KEY,
  snapshot_digest TEXT NOT NULL REFERENCES landscape_snapshot(digest),
  role            TEXT NOT NULL,              -- 'creator'|'judge'|'executor'
  schema_version  INTEGER NOT NULL,
  manifest        TEXT,                       -- JSON canonical body; NULL → CAS file
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pack_snapshot ON context_pack(snapshot_digest);

CREATE TABLE IF NOT EXISTS workflow_spec (    -- typed, judgeable plan artifact
  digest          TEXT PRIMARY KEY,
  project_id      INTEGER NOT NULL REFERENCES project(id),
  name            TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  spec            TEXT,                       -- JSON canonical body; NULL → CAS file
  pack_digest     TEXT REFERENCES context_pack(digest),
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_spec_project ON workflow_spec(project_id);

-- Immutability guards: digest-keyed rows are insert-only. DELETE is
-- reserved for explicit GC tooling (ADR 0002 §4), never application code.
CREATE TRIGGER IF NOT EXISTS evidence_ref_immutable
  BEFORE UPDATE ON evidence_ref
  BEGIN SELECT RAISE(ABORT, 'evidence_ref rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS landscape_snapshot_immutable
  BEFORE UPDATE ON landscape_snapshot
  BEGIN SELECT RAISE(ABORT, 'landscape_snapshot rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS snapshot_evidence_immutable
  BEFORE UPDATE ON snapshot_evidence
  BEGIN SELECT RAISE(ABORT, 'snapshot_evidence rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS context_pack_immutable
  BEFORE UPDATE ON context_pack
  BEGIN SELECT RAISE(ABORT, 'context_pack rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS workflow_spec_immutable
  BEFORE UPDATE ON workflow_spec
  BEGIN SELECT RAISE(ABORT, 'workflow_spec rows are immutable'); END;

-- NOTE: workflow_run (Temporal projection) was deferred until the Temporal
-- spike landed (b9585be); it is defined below (ADR 0001 §4 step 6).

-- ── Mutable projection of Temporal state (ADR 0001 §4 step 6, wave 4) ──
-- Temporal is the source of truth for execution state; this table is a
-- queryable projection for the LiveView audit UI, written ONLY by Python
-- Temporal Activities (optimizer_run precedent). spec_digest is nullable —
-- deliberate deviation from the ADR sketch: release workflows keyed by
-- release:<repo>:<commit> may start before a WorkflowSpec artifact exists.
CREATE TABLE IF NOT EXISTS workflow_run (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_digest          TEXT REFERENCES workflow_spec(digest),
  temporal_workflow_id TEXT NOT NULL,
  temporal_run_id      TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'running',
                       -- 'running'|'awaiting-approval'|'done'|'failed'
                       -- |'canceled'|'rolled-back'
  environment_id       INTEGER REFERENCES environment(id),
  detail               TEXT NOT NULL DEFAULT '{}',  -- JSON: stage, error, timings
  stage_history        TEXT NOT NULL DEFAULT '[]',  -- JSON: append-only [{stage,status,at}]
  started_at           TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at          TEXT,
  UNIQUE(temporal_workflow_id, temporal_run_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_run_status ON workflow_run(status);
CREATE INDEX IF NOT EXISTS idx_workflow_run_wfid ON workflow_run(temporal_workflow_id);

-- ── Approval audit (wave 7) ─────────────────────────────────────────────
-- Append-only record of every human approval decision delivered to a
-- workflow. Written by the approval gateway (bin/approvals.py) at the
-- moment the Signal is sent; NEVER updated — corrections are new rows.
-- The approver allowlist lives in the policy table (kind='approval').
CREATE TABLE IF NOT EXISTS approval_event (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  temporal_workflow_id TEXT NOT NULL,
  decision             TEXT NOT NULL,        -- 'approved'|'rejected'
  approver             TEXT NOT NULL,        -- authenticated principal
  reason               TEXT NOT NULL DEFAULT '',
  policy_id            INTEGER REFERENCES policy(id),
  created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_approval_event_wfid
  ON approval_event(temporal_workflow_id);
CREATE TRIGGER IF NOT EXISTS approval_event_immutable
  BEFORE UPDATE ON approval_event
  BEGIN SELECT RAISE(ABORT, 'approval_event rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS approval_event_no_delete
  BEFORE DELETE ON approval_event
  BEGIN SELECT RAISE(ABORT, 'approval_event rows are append-only'); END;

-- ═══════════════════════════════════════════════════════════════════════
-- Campaign / finding spine (wave-8 B4; docs/reviews/bugsweep-product-model.md).
-- Campaigns and findings are MUTABLE catalog-style rows — they accrete
-- state over a campaign's life. Their evidence links are digests into the
-- immutable evidence_ref table. review_lens_verdict and validation_ledger
-- are APPEND-ONLY histories (corrections are new rows), mirroring
-- approval_event. Python (bin/campaigns.py) is the writer.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS campaign (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  name         TEXT NOT NULL UNIQUE,           -- e.g. 'bugsweep-aug16'
  kind         TEXT NOT NULL CHECK (kind IN ('bugsweep','release')),
  objective    TEXT NOT NULL DEFAULT '',       -- charter: target, scope
  time_window  TEXT NOT NULL DEFAULT '',       -- e.g. 'sentry 7d + slack 14d'
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  base_commit  TEXT NOT NULL DEFAULT '',       -- the pin: analysis/patch/validation base
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_campaign_project ON campaign(project_id);

-- One intake-ledger row per candidate. States: in-flight
-- (candidate..published) then terminal (confirmed_validated | failed_infra
-- | no_go). FAILED ≠ NO_GO: failed_infra is a lane-infrastructure fault
-- (re-queue candidate); no_go is a documented terminal deliverable and
-- REQUIRES a reason from the 5-class taxonomy.
CREATE TABLE IF NOT EXISTS finding (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id    INTEGER NOT NULL REFERENCES campaign(id),
  slug           TEXT NOT NULL,                -- lane name, e.g. 'chat-scroll-jump-nre'
  title          TEXT NOT NULL DEFAULT '',
  source_kind    TEXT NOT NULL DEFAULT '',     -- signal provenance: sentry|slack|autoclosed|perf|...
  state          TEXT NOT NULL DEFAULT 'candidate' CHECK (state IN (
                   'candidate','investigating','workorder_generated','judged',
                   'approved','executing','published',
                   'confirmed_validated','failed_infra','no_go')),
  no_go_reason   TEXT CHECK (no_go_reason IS NULL OR no_go_reason IN (
                   'already-fixed','wrong-repo','by-design',
                   'stale-signal','insufficient-evidence')),
  root_cause     TEXT NOT NULL DEFAULT '',     -- one-liner for the ledger
  tracking_links TEXT NOT NULL DEFAULT '[]',   -- JSON: [{kind: fixes|related|..., ref}]
  dedup_notes    TEXT NOT NULL DEFAULT '',     -- dedup proof (lists checked, hits)
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(campaign_id, slug),
  CHECK (state <> 'no_go' OR no_go_reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_finding_campaign ON finding(campaign_id);
CREATE INDEX IF NOT EXISTS idx_finding_state ON finding(campaign_id, state);

-- Evidence links: digests into the immutable evidence_ref table, with the
-- role the evidence plays in the finding's dossier.
CREATE TABLE IF NOT EXISTS finding_evidence (
  finding_id      INTEGER NOT NULL REFERENCES finding(id),
  evidence_digest TEXT NOT NULL REFERENCES evidence_ref(digest),
  role            TEXT NOT NULL DEFAULT 'signal'
                  CHECK (role IN ('signal','root-cause','dedup','validation')),
  PRIMARY KEY (finding_id, evidence_digest, role)
);
CREATE INDEX IF NOT EXISTS idx_finding_evidence_finding
  ON finding_evidence(finding_id);

-- Per-lens adversarial review verdicts (root-cause / lifecycle-regression /
-- ecs-struct-perf / ...). APPEND-ONLY: review.md is an accumulating
-- transcript; corrections are new rows. repair_of implements the
-- one-repair-cycle loop: a REFUTE may be answered by AT MOST ONE later
-- verdict row pointing back at it (enforced in bin/campaigns.py — the
-- repair target must be a REFUTE, must not itself be a repair, and must
-- not already have a repair).
CREATE TABLE IF NOT EXISTS review_lens_verdict (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id  INTEGER NOT NULL REFERENCES finding(id),
  lens        TEXT NOT NULL,
  verdict     TEXT NOT NULL CHECK (verdict IN ('CONFIRM','REFUTE')),
  rationale   TEXT NOT NULL DEFAULT '',
  repair_of   INTEGER REFERENCES review_lens_verdict(id),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lens_verdict_finding
  ON review_lens_verdict(finding_id);
CREATE TRIGGER IF NOT EXISTS review_lens_verdict_immutable
  BEFORE UPDATE ON review_lens_verdict
  BEGIN SELECT RAISE(ABORT, 'review_lens_verdict rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_lens_verdict_no_delete
  BEFORE DELETE ON review_lens_verdict
  BEGIN SELECT RAISE(ABORT, 'review_lens_verdict rows are append-only'); END;

-- RED/GREEN validation ledger. "VALIDATED" is never a boolean: RED legs
-- record intended-failure fractions (red_observed of red_intended failed by
-- their INTENDED assertion), guard tests pass both ways, harness fixes are
-- ledgered in harness_notes. APPEND-ONLY: each validation run is a new row.
CREATE TABLE IF NOT EXISTS validation_ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id    INTEGER NOT NULL REFERENCES finding(id),
  red_intended  INTEGER NOT NULL DEFAULT 0,   -- tests that MUST fail at pin
  red_observed  INTEGER NOT NULL DEFAULT 0,   -- how many failed by intended assertion
  green_total   INTEGER NOT NULL DEFAULT 0,
  green_passed  INTEGER NOT NULL DEFAULT 0,
  guards        INTEGER NOT NULL DEFAULT 0,   -- guard tests (pass both ways)
  harness_notes TEXT NOT NULL DEFAULT '{}',   -- JSON: {tag, host, harness_fixes[], ...}
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_validation_ledger_finding
  ON validation_ledger(finding_id);
CREATE TRIGGER IF NOT EXISTS validation_ledger_immutable
  BEFORE UPDATE ON validation_ledger
  BEGIN SELECT RAISE(ABORT, 'validation_ledger rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS validation_ledger_no_delete
  BEFORE DELETE ON validation_ledger
  BEGIN SELECT RAISE(ABORT, 'validation_ledger rows are append-only'); END;

-- ═══════════════════════════════════════════════════════════════════════
-- Developer-opinion learning loop (wave-8 B5;
-- docs/reviews/bugsweep-product-model.md §4.3). Review-rule proposals are
-- MUTABLE while draft/evaluated (they accrete evaluation evidence); on
-- human-gated promotion the text/evidence/attribution/dissent are FROZEN
-- into an IMMUTABLE versioned review_rule row (mirrors skill(name,
-- version) / eval_template(name, version)). Raw comments and frequency
-- counts NEVER auto-promote: promotion requires an authenticated
-- principal authorized via bin.approvals.authorize plus an approval_event
-- audit row. Python (bin/review_rules.py) is the writer.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS review_rule_proposal (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  category            TEXT NOT NULL,            -- perf-alloc|ecs-lifecycle|nullability|...
  rule_text           TEXT NOT NULL,            -- imperative; sub-forms enumerated
  sub_forms           TEXT NOT NULL DEFAULT '[]',  -- JSON list[str]
  approx_frequency    INTEGER NOT NULL DEFAULT 0,  -- ≈count over the mining window
  window              TEXT NOT NULL DEFAULT '',    -- e.g. '2026-02-16..2026-08-16'
  blocking_class      TEXT NOT NULL DEFAULT 'N'
                      CHECK (blocking_class IN ('B','N','mixed')),  -- OBSERVED, not declared
  written_status      TEXT NOT NULL DEFAULT 'unwritten',  -- written|unwritten|partial
  doc_pointer         TEXT NOT NULL DEFAULT '',  -- 'CLAUDE.md:133', ...
  top_enforcers       TEXT NOT NULL DEFAULT '[]',  -- JSON list[str], ranked attribution
  evidence            TEXT NOT NULL DEFAULT '[]',  -- JSON list[RuleEvidence]; ≥2 quotes required
  exceptions          TEXT NOT NULL DEFAULT '[]',  -- JSON list[RuleException]
  conflicts_with      TEXT NOT NULL DEFAULT '[]',  -- JSON list[str]
  dissent             TEXT NOT NULL DEFAULT '[]',  -- JSON list[DissentingOpinion]; carried, never erased
  mechanization       TEXT NOT NULL DEFAULT '{}',  -- JSON MechanizationRoute
  application_targets TEXT NOT NULL DEFAULT '[]',  -- JSON list[str] (SKILL-DELTA targets)
  status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                        'draft','evaluated','approved','versioned','rejected')),
  evaluated_by        TEXT NOT NULL DEFAULT '',  -- judge run / corpus back-test reference
  evaluation_result   TEXT NOT NULL DEFAULT '',  -- e.g. 'retro: 24 diffs, 7 BLOCK hits, 0 FPs'
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rule_proposal_status
  ON review_rule_proposal(status);
CREATE INDEX IF NOT EXISTS idx_rule_proposal_category
  ON review_rule_proposal(category);

-- Versioned, IMMUTABLE promoted rules. Old versions remain (rollback =
-- repoint). supersedes names the prior 'name:vN' this version replaces.
CREATE TABLE IF NOT EXISTS review_rule (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT NOT NULL,
  version           INTEGER NOT NULL,
  category          TEXT NOT NULL,
  rule_text         TEXT NOT NULL,              -- frozen at approval time
  evidence          TEXT NOT NULL DEFAULT '[]', -- frozen JSON list[RuleEvidence]
  attribution       TEXT NOT NULL DEFAULT '[]', -- frozen JSON top_enforcers
  dissent           TEXT NOT NULL DEFAULT '[]', -- frozen JSON; carried, never erased
  proposal_id       INTEGER NOT NULL REFERENCES review_rule_proposal(id),
  approved_by       TEXT NOT NULL,              -- authenticated principal
  approval_event_id INTEGER NOT NULL REFERENCES approval_event(id),
  supersedes        TEXT,                       -- 'name:vN' of the replaced version, or NULL
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_review_rule_name ON review_rule(name);
CREATE TRIGGER IF NOT EXISTS review_rule_immutable
  BEFORE UPDATE ON review_rule
  BEGIN SELECT RAISE(ABORT, 'review_rule rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_rule_no_delete
  BEFORE DELETE ON review_rule
  BEGIN SELECT RAISE(ABORT, 'review_rule rows are append-only'); END;

-- ═══════════════════════════════════════════════════════════════════════
-- Branch-fix spine (wave-9 B1+B2). Every fix lives on ONE branch; every
-- validation/review row binds to ONE exact head SHA. Validation of an
-- aggregate/merged tree is impossible by construction: the validator
-- checks out a detached worktree at head_sha and the validation row
-- stores tested_sha; a head change strands prior rows (they reference
-- the old SHA) and refresh marks the branch 'stale'.
--   fix_branch            — MUTABLE registration row (status accretes).
--   fix_branch_validation — APPEND-ONLY per-SHA validation runs.
--   fix_branch_review     — APPEND-ONLY per-SHA review decisions; approve
--                           requires an approval_event audit row (RBAC via
--                           bin.approvals.authorize, fail closed).
-- Python (bin/fix_branches.py, bin/branch_validator.py) is the writer.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS fix_branch (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id    INTEGER REFERENCES finding(id),
  workorder_ref TEXT,                          -- pending_judgement id or slug
  repo_path     TEXT NOT NULL,
  branch_name   TEXT NOT NULL,
  base_sha      TEXT NOT NULL,                 -- merge-base with default/explicit base
  head_sha      TEXT NOT NULL,                 -- branch tip at registration/refresh
  patch_digest  TEXT,                          -- sha256 of `git diff base..head`
  status        TEXT NOT NULL DEFAULT 'registered' CHECK (status IN (
                  'registered','validating','validated','failed',
                  'stale','approved','rejected','shipping','shipped',
                  'rolled-back')),
  created_at    TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(repo_path, branch_name)
);
CREATE INDEX IF NOT EXISTS idx_fix_branch_finding ON fix_branch(finding_id);
CREATE INDEX IF NOT EXISTS idx_fix_branch_status ON fix_branch(status);

-- One row per validation RUN of one exact SHA. tested_sha is the detached
-- worktree's HEAD; writes are refused in code when it no longer matches
-- fix_branch.head_sha (staleness guard at write time).
CREATE TABLE IF NOT EXISTS fix_branch_validation (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  fix_branch_id INTEGER NOT NULL REFERENCES fix_branch(id),
  tested_sha    TEXT NOT NULL,
  red_cmd       TEXT,
  red_intended  INTEGER,
  red_observed  INTEGER,
  green_cmd     TEXT,
  green_total   INTEGER,
  green_passed  INTEGER,
  guard_total   INTEGER,
  guard_passed  INTEGER,
  passed        INTEGER NOT NULL CHECK (passed IN (0,1)),
  log_digest    TEXT,                          -- sha256 of the combined run log (CAS)
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fix_branch_validation_branch
  ON fix_branch_validation(fix_branch_id);
CREATE TRIGGER IF NOT EXISTS fix_branch_validation_immutable
  BEFORE UPDATE ON fix_branch_validation
  BEGIN SELECT RAISE(ABORT, 'fix_branch_validation rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fix_branch_validation_no_delete
  BEFORE DELETE ON fix_branch_validation
  BEGIN SELECT RAISE(ABORT, 'fix_branch_validation rows are append-only'); END;

-- One row per review decision of one exact SHA. approve rows carry the
-- approval_event audit id (RBAC enforced in bin/fix_branches.py BEFORE the
-- row is written; ApprovalDenied propagates — fail closed).
CREATE TABLE IF NOT EXISTS fix_branch_review (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  fix_branch_id     INTEGER NOT NULL REFERENCES fix_branch(id),
  tested_sha        TEXT NOT NULL,
  reviewer          TEXT NOT NULL,
  decision          TEXT NOT NULL CHECK (decision IN (
                      'approve','reject','needs-changes')),
  rationale         TEXT NOT NULL DEFAULT '',
  approval_event_id INTEGER REFERENCES approval_event(id),
  created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fix_branch_review_branch
  ON fix_branch_review(fix_branch_id);
CREATE TRIGGER IF NOT EXISTS fix_branch_review_immutable
  BEFORE UPDATE ON fix_branch_review
  BEGIN SELECT RAISE(ABORT, 'fix_branch_review rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fix_branch_review_no_delete
  BEFORE DELETE ON fix_branch_review
  BEGIN SELECT RAISE(ABORT, 'fix_branch_review rows are append-only'); END;

-- One row per ship ATTEMPT accepted by the gateway (wave-11 W2): which
-- release workflow was started, for which exact tested SHA, on whose
-- request, and which approval review / validation run justified it.
-- 'shipped' on fix_branch now means release-COMPLETED — the gateway sets
-- 'shipping' when the workflow starts and branch_ship.sync_completion
-- projects the workflow_run outcome (done -> shipped, rolled-back ->
-- rolled-back). APPEND-ONLY (approval_event precedent): ship history is
-- evidence; corrections are new rows. Python (bin/fix_branches.py via
-- bin/branch_ship.py) is the writer.
CREATE TABLE IF NOT EXISTS fix_branch_ship (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  fix_branch_id      INTEGER NOT NULL REFERENCES fix_branch(id),
  workflow_id        TEXT NOT NULL,        -- temporal_workflow_id the client started
  tested_sha         TEXT NOT NULL,        -- exact head SHA that shipped
  requested_by       TEXT NOT NULL,
  approval_review_id INTEGER REFERENCES fix_branch_review(id),
  validation_id      INTEGER REFERENCES fix_branch_validation(id),
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_fix_branch_ship_branch
  ON fix_branch_ship(fix_branch_id);
CREATE TRIGGER IF NOT EXISTS fix_branch_ship_immutable
  BEFORE UPDATE ON fix_branch_ship
  BEGIN SELECT RAISE(ABORT, 'fix_branch_ship rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS fix_branch_ship_no_delete
  BEFORE DELETE ON fix_branch_ship
  BEGIN SELECT RAISE(ABORT, 'fix_branch_ship rows are append-only'); END;

-- ═══════════════════════════════════════════════════════════════════════
-- Branch authoring provenance (wave-10 V1). One row per AUTHORED branch:
-- which backend produced the patch ('fixture' = deterministic file
-- content, 'command' = configured command, e.g. a coding agent), for
-- which workorder, bound to the EXACT base and head SHAs and the patch
-- digest at authoring time. provenance is a JSON summary of the backend
-- config (fixture label + file list, or command argv) — NEVER secrets.
-- APPEND-ONLY (approval_event precedent): authoring history is evidence;
-- corrections are new rows. Python (bin/branch_author.py) is the writer.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS branch_authoring (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  fix_branch_id INTEGER NOT NULL REFERENCES fix_branch(id),
  backend       TEXT NOT NULL CHECK (backend IN ('fixture','command')),
  workorder_ref TEXT,                          -- pending_judgement id or slug
  base_sha      TEXT NOT NULL,                 -- immutable authoring base
  head_sha      TEXT NOT NULL,                 -- exact commit the backend produced
  patch_digest  TEXT NOT NULL,                 -- sha256 of `git diff base..head`
  provenance    TEXT,                          -- JSON backend-config summary; NEVER secrets
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_branch_authoring_branch
  ON branch_authoring(fix_branch_id);
CREATE TRIGGER IF NOT EXISTS branch_authoring_immutable
  BEFORE UPDATE ON branch_authoring
  BEGIN SELECT RAISE(ABORT, 'branch_authoring rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS branch_authoring_no_delete
  BEFORE DELETE ON branch_authoring
  BEGIN SELECT RAISE(ABORT, 'branch_authoring rows are append-only'); END;

-- ═══════════════════════════════════════════════════════════════════════
-- Pipeline spec v1 (wave-12; docs/design/judgement-wheel-v2.md §4).
-- A pipeline is a DECLARATIVE, versioned, IMMUTABLE spec: ordered stages,
-- each binding either (subject, judgement, rubric) or a platform action.
-- v1 is a SPEC + registry, NOT an executor. Validation (stage shape,
-- rubric existence, the fail-closed release gate) happens in
-- bin/pipelines.py at REGISTRATION time; the DB stores only validated,
-- canonicalized definitions. Changes create a NEW version (eval_template
-- (name, version) precedent); rows are append-only (approval_event
-- precedent).
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS pipeline (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT    NOT NULL,
  version           INTEGER NOT NULL,
  definition        TEXT    NOT NULL,   -- canonical JSON (sorted keys, compact)
  definition_digest TEXT    NOT NULL,   -- sha256 hex of the canonical JSON
  created_at        TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_pipeline_name ON pipeline(name);
CREATE TRIGGER IF NOT EXISTS pipeline_immutable
  BEFORE UPDATE ON pipeline
  BEGIN SELECT RAISE(ABORT, 'pipeline rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS pipeline_no_delete
  BEFORE DELETE ON pipeline
  BEGIN SELECT RAISE(ABORT, 'pipeline rows are append-only'); END;

-- Domain → (pipeline, version) binding. SEMANTICS (deliberate, documented):
-- a binding is PERMANENT for a domain — UNIQUE(domain_id) plus the
-- append-only triggers below mean a domain binds exactly once, forever.
-- Rebinding to a different pipeline/version requires a NEW domain (or a
-- future versioned rebind table if that need ever materializes). This is
-- the simplest honest choice: the binding is evidence about how a
-- domain's judgements were produced, so it must never drift under
-- already-recorded work.
CREATE TABLE IF NOT EXISTS domain_pipeline (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  domain_id   INTEGER NOT NULL REFERENCES domain(id),
  pipeline_id INTEGER NOT NULL REFERENCES pipeline(id),
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  UNIQUE(domain_id)
);
CREATE INDEX IF NOT EXISTS idx_domain_pipeline_pipeline
  ON domain_pipeline(pipeline_id);
CREATE TRIGGER IF NOT EXISTS domain_pipeline_immutable
  BEFORE UPDATE ON domain_pipeline
  BEGIN SELECT RAISE(ABORT, 'domain_pipeline rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS domain_pipeline_no_delete
  BEFORE DELETE ON domain_pipeline
  BEGIN SELECT RAISE(ABORT, 'domain_pipeline rows are append-only'); END;

-- ═══════════════════════════════════════════════════════════════════════
-- Judgement revision (wave-13 slice A; operator-environment-v13 §1).
-- "Go back and edit judging" NEVER mutates or deletes score rows: revising
-- writes a brand-new rating (new rating_id + fresh score rows via the same
-- write path, against the SAME pending row, which stays 'done') plus ONE
-- revision row here linking previous -> new. The effective verdict for a
-- pending is the tip of this chain; superseded ratings remain forever as
-- evidence. Downstream outcomes already derived from an old verdict are
-- NOT rewritten. APPEND-ONLY (approval_event precedent): corrections are
-- new rows. Writers: bin/judgement.py (revise_judgement) and the LiveView
-- adapter (TournamentUi.Judgement.revise_human) — same contract.
-- ═══════════════════════════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS judgement_revision (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  pending_id         INTEGER NOT NULL REFERENCES pending_judgement(id),
  previous_rating_id TEXT    NOT NULL,   -- the rating this revision supersedes
  new_rating_id      TEXT    NOT NULL,   -- the freshly written rating
  revised_by         TEXT    NOT NULL,
  reason             TEXT    NOT NULL,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_judgement_revision_pending
  ON judgement_revision(pending_id);
CREATE TRIGGER IF NOT EXISTS judgement_revision_immutable
  BEFORE UPDATE ON judgement_revision
  BEGIN SELECT RAISE(ABORT, 'judgement_revision rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS judgement_revision_no_delete
  BEFORE DELETE ON judgement_revision
  BEGIN SELECT RAISE(ABORT, 'judgement_revision rows are append-only'); END;
