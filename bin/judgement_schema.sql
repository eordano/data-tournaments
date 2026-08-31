
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS eval_template (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT    NOT NULL,
  version         INTEGER NOT NULL,
  output_definition TEXT  NOT NULL,
  langfuse_prompt_name TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  is_draft        INTEGER NOT NULL DEFAULT 0,
  UNIQUE(name, version)
);
CREATE INDEX IF NOT EXISTS idx_eval_template_name ON eval_template(name);

CREATE TABLE IF NOT EXISTS job_configuration (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  template_id     INTEGER NOT NULL REFERENCES eval_template(id),
  rater_type      TEXT    NOT NULL,
  rater_config    TEXT    NOT NULL DEFAULT '{}',
  sampling        REAL    NOT NULL DEFAULT 1.0,
  status          TEXT    NOT NULL DEFAULT 'active',
  blocked_at      TEXT,
  block_reason    TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_config_active
  ON job_configuration(status, rater_type);

CREATE TABLE IF NOT EXISTS pending_judgement (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  config_id       INTEGER NOT NULL REFERENCES job_configuration(id),
  tournament_db_path TEXT NOT NULL,
  match_id        INTEGER NOT NULL,
  trace_id        TEXT,
  trace_payload   TEXT    NOT NULL,
  pair_key        TEXT,
  content_a       TEXT,
  content_b       TEXT,
  status          TEXT    NOT NULL DEFAULT 'pending',
  rating_id       TEXT,
  error_message   TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  completed_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_pending_status
  ON pending_judgement(status, config_id);
CREATE INDEX IF NOT EXISTS idx_pending_trace
  ON pending_judgement(tournament_db_path, match_id);

CREATE TABLE IF NOT EXISTS score (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  rating_id       TEXT    NOT NULL,
  pending_id      INTEGER REFERENCES pending_judgement(id),
  template_id     INTEGER NOT NULL REFERENCES eval_template(id),
  rubric_version  INTEGER NOT NULL,
  name            TEXT    NOT NULL,
  data_type       TEXT    NOT NULL,
  value           TEXT    NOT NULL,
  metadata        TEXT    NOT NULL DEFAULT '{}',
  tournament_db_path TEXT NOT NULL,
  match_id        INTEGER NOT NULL,
  trace_id        TEXT,
  pair_key        TEXT,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_score_rating ON score(rating_id);
CREATE INDEX IF NOT EXISTS idx_score_name_value ON score(name, value);
CREATE INDEX IF NOT EXISTS idx_score_trace
  ON score(tournament_db_path, match_id);
CREATE INDEX IF NOT EXISTS idx_score_template ON score(template_id);

CREATE TABLE IF NOT EXISTS domain (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT    NOT NULL UNIQUE,
  description       TEXT    NOT NULL DEFAULT '',
  generator_prompt  TEXT    NOT NULL,
  judge_prompt      TEXT    NOT NULL,
  rubric            TEXT    NOT NULL DEFAULT 'pair-wheel-v2',
  corpus_source     TEXT    NOT NULL,
  status            TEXT    NOT NULL DEFAULT 'active',
  created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_domain_status ON domain(status);

CREATE TABLE IF NOT EXISTS project (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  description  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'active',
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS component (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_component_project ON component(project_id);

CREATE TABLE IF NOT EXISTS source (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  name         TEXT NOT NULL,
  kind         TEXT NOT NULL,
  locator      TEXT NOT NULL,
  trust_tier   INTEGER NOT NULL DEFAULT 3,
  config       TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(project_id, name)
);
CREATE INDEX IF NOT EXISTS idx_source_project ON source(project_id);

CREATE TABLE IF NOT EXISTS capability (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  description  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skill (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL,
  version      INTEGER NOT NULL,
  locator      TEXT NOT NULL,
  digest       TEXT,
  status       TEXT NOT NULL DEFAULT 'active',
  metadata     TEXT NOT NULL DEFAULT '{}',
  created_at   TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(name, version)
);

CREATE TABLE IF NOT EXISTS environment (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL,
  config       TEXT NOT NULL DEFAULT '{}',
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS policy (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  name         TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL,
  rule         TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'active',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

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

CREATE TABLE IF NOT EXISTS evidence_ref (
  digest       TEXT PRIMARY KEY,
  source_id    INTEGER NOT NULL REFERENCES source(id),
  kind         TEXT NOT NULL,
  locator      TEXT NOT NULL,
  trust_tier   INTEGER NOT NULL,
  summary      TEXT NOT NULL DEFAULT '',
  body         TEXT,
  captured_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_evidence_source ON evidence_ref(source_id);

CREATE TABLE IF NOT EXISTS landscape_snapshot (
  digest       TEXT PRIMARY KEY,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  schema_version INTEGER NOT NULL,
  manifest     TEXT,
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshot_project ON landscape_snapshot(project_id);

CREATE TABLE IF NOT EXISTS snapshot_evidence (
  snapshot_digest TEXT NOT NULL REFERENCES landscape_snapshot(digest),
  evidence_digest TEXT NOT NULL REFERENCES evidence_ref(digest),
  PRIMARY KEY (snapshot_digest, evidence_digest)
);

CREATE TABLE IF NOT EXISTS context_pack (
  digest          TEXT PRIMARY KEY,
  snapshot_digest TEXT NOT NULL REFERENCES landscape_snapshot(digest),
  role            TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  manifest        TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_pack_snapshot ON context_pack(snapshot_digest);

CREATE TABLE IF NOT EXISTS workflow_spec (
  digest          TEXT PRIMARY KEY,
  project_id      INTEGER NOT NULL REFERENCES project(id),
  name            TEXT NOT NULL,
  schema_version  INTEGER NOT NULL,
  spec            TEXT,
  pack_digest     TEXT REFERENCES context_pack(digest),
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_spec_project ON workflow_spec(project_id);

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

CREATE TABLE IF NOT EXISTS workflow_run (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  spec_digest          TEXT REFERENCES workflow_spec(digest),
  temporal_workflow_id TEXT NOT NULL,
  temporal_run_id      TEXT NOT NULL,
  status               TEXT NOT NULL DEFAULT 'running',
  environment_id       INTEGER REFERENCES environment(id),
  detail               TEXT NOT NULL DEFAULT '{}',
  stage_history        TEXT NOT NULL DEFAULT '[]',
  started_at           TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at          TEXT,
  UNIQUE(temporal_workflow_id, temporal_run_id)
);
CREATE INDEX IF NOT EXISTS idx_workflow_run_status ON workflow_run(status);
CREATE INDEX IF NOT EXISTS idx_workflow_run_wfid ON workflow_run(temporal_workflow_id);

CREATE TABLE IF NOT EXISTS approval_event (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  temporal_workflow_id TEXT NOT NULL,
  decision             TEXT NOT NULL,
  approver             TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS campaign (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  project_id   INTEGER NOT NULL REFERENCES project(id),
  name         TEXT NOT NULL UNIQUE,
  kind         TEXT NOT NULL CHECK (kind IN
                 ('bugsweep','perfsweep','featuresweep','slopsweep','release')),
  objective    TEXT NOT NULL DEFAULT '',
  time_window  TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','closed')),
  base_commit  TEXT NOT NULL DEFAULT '',
  spec_json    TEXT NOT NULL DEFAULT '',
  spec_digest  TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_campaign_project ON campaign(project_id);

CREATE TABLE IF NOT EXISTS sweep_round (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id    INTEGER NOT NULL REFERENCES campaign(id),
  round_no       INTEGER NOT NULL,
  status         TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  outcome        TEXT CHECK (outcome IS NULL OR outcome IN
                   ('converged','not_converged')),
  summary        TEXT NOT NULL DEFAULT '{}',
  dataset_run_id TEXT NOT NULL DEFAULT '',
  opened_at      TEXT NOT NULL DEFAULT (datetime('now')),
  closed_at      TEXT,
  UNIQUE(campaign_id, round_no),
  CHECK (status = 'open' OR outcome IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_sweep_round_campaign ON sweep_round(campaign_id);

CREATE TABLE IF NOT EXISTS finding_disposition (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id  INTEGER NOT NULL REFERENCES finding(id),
  decision    TEXT NOT NULL CHECK (decision IN ('ship_anyway','needs_fix','no_go')),
  rationale   TEXT NOT NULL,
  decided_by  TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_finding_disposition_finding
  ON finding_disposition(finding_id);
CREATE TRIGGER IF NOT EXISTS finding_disposition_immutable
  BEFORE UPDATE ON finding_disposition
  BEGIN SELECT RAISE(ABORT, 'finding_disposition rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS finding_disposition_no_delete
  BEFORE DELETE ON finding_disposition
  BEGIN SELECT RAISE(ABORT, 'finding_disposition rows are append-only'); END;

CREATE TABLE IF NOT EXISTS finding (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  campaign_id    INTEGER NOT NULL REFERENCES campaign(id),
  slug           TEXT NOT NULL,
  title          TEXT NOT NULL DEFAULT '',
  source_kind    TEXT NOT NULL DEFAULT '',
  state          TEXT NOT NULL DEFAULT 'candidate' CHECK (state IN (
                   'candidate','investigating','workorder_generated','judged',
                   'approved','executing','published',
                   'confirmed_validated','failed_infra','no_go')),
  no_go_reason   TEXT CHECK (no_go_reason IS NULL OR no_go_reason IN (
                   'already-fixed','wrong-repo','by-design',
                   'stale-signal','insufficient-evidence')),
  root_cause     TEXT NOT NULL DEFAULT '',
  tracking_links TEXT NOT NULL DEFAULT '[]',
  dedup_notes    TEXT NOT NULL DEFAULT '',
  created_at     TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(campaign_id, slug),
  CHECK (state <> 'no_go' OR no_go_reason IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_finding_campaign ON finding(campaign_id);
CREATE INDEX IF NOT EXISTS idx_finding_state ON finding(campaign_id, state);

CREATE TABLE IF NOT EXISTS finding_evidence (
  finding_id      INTEGER NOT NULL REFERENCES finding(id),
  evidence_digest TEXT NOT NULL REFERENCES evidence_ref(digest),
  role            TEXT NOT NULL DEFAULT 'signal'
                  CHECK (role IN ('signal','root-cause','dedup','validation')),
  PRIMARY KEY (finding_id, evidence_digest, role)
);
CREATE INDEX IF NOT EXISTS idx_finding_evidence_finding
  ON finding_evidence(finding_id);

CREATE TABLE IF NOT EXISTS review_lens_verdict (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id  INTEGER NOT NULL REFERENCES finding(id),
  lens        TEXT NOT NULL,
  verdict     TEXT NOT NULL CHECK (verdict IN ('CONFIRM','REFUTE')),
  rationale   TEXT NOT NULL DEFAULT '',
  repair_of   INTEGER REFERENCES review_lens_verdict(id),
  round_id    INTEGER REFERENCES sweep_round(id),
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_lens_verdict_finding
  ON review_lens_verdict(finding_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lens_verdict_repair_once
  ON review_lens_verdict(repair_of) WHERE repair_of IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS review_lens_verdict_round_open
  BEFORE INSERT ON review_lens_verdict
  WHEN NEW.round_id IS NOT NULL AND (
    SELECT status FROM sweep_round WHERE id = NEW.round_id
  ) <> 'open'
  BEGIN SELECT RAISE(ABORT, 'verdict round is not open'); END;
CREATE TRIGGER IF NOT EXISTS review_lens_verdict_immutable
  BEFORE UPDATE ON review_lens_verdict
  BEGIN SELECT RAISE(ABORT, 'review_lens_verdict rows are immutable'); END;
CREATE TRIGGER IF NOT EXISTS review_lens_verdict_no_delete
  BEFORE DELETE ON review_lens_verdict
  BEGIN SELECT RAISE(ABORT, 'review_lens_verdict rows are append-only'); END;

CREATE TABLE IF NOT EXISTS validation_ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id    INTEGER NOT NULL REFERENCES finding(id),
  red_intended  INTEGER NOT NULL DEFAULT 0,
  red_observed  INTEGER NOT NULL DEFAULT 0,
  green_total   INTEGER NOT NULL DEFAULT 0,
  green_passed  INTEGER NOT NULL DEFAULT 0,
  guards        INTEGER NOT NULL DEFAULT 0,
  harness_notes TEXT NOT NULL DEFAULT '{}',
  perf_json     TEXT NOT NULL DEFAULT '[]',
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

CREATE TABLE IF NOT EXISTS review_rule_proposal (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  category            TEXT NOT NULL,
  rule_text           TEXT NOT NULL,
  sub_forms           TEXT NOT NULL DEFAULT '[]',
  approx_frequency    INTEGER NOT NULL DEFAULT 0,
  window              TEXT NOT NULL DEFAULT '',
  blocking_class      TEXT NOT NULL DEFAULT 'N'
                      CHECK (blocking_class IN ('B','N','mixed')),
  written_status      TEXT NOT NULL DEFAULT 'unwritten',
  doc_pointer         TEXT NOT NULL DEFAULT '',
  top_enforcers       TEXT NOT NULL DEFAULT '[]',
  evidence            TEXT NOT NULL DEFAULT '[]',
  exceptions          TEXT NOT NULL DEFAULT '[]',
  conflicts_with      TEXT NOT NULL DEFAULT '[]',
  dissent             TEXT NOT NULL DEFAULT '[]',
  mechanization       TEXT NOT NULL DEFAULT '{}',
  application_targets TEXT NOT NULL DEFAULT '[]',
  status              TEXT NOT NULL DEFAULT 'draft' CHECK (status IN (
                        'draft','evaluated','approved','versioned','rejected')),
  evaluated_by        TEXT NOT NULL DEFAULT '',
  evaluation_result   TEXT NOT NULL DEFAULT '',
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_rule_proposal_status
  ON review_rule_proposal(status);
CREATE INDEX IF NOT EXISTS idx_rule_proposal_category
  ON review_rule_proposal(category);

CREATE TABLE IF NOT EXISTS review_rule (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT NOT NULL,
  version           INTEGER NOT NULL,
  category          TEXT NOT NULL,
  rule_text         TEXT NOT NULL,
  evidence          TEXT NOT NULL DEFAULT '[]',
  attribution       TEXT NOT NULL DEFAULT '[]',
  dissent           TEXT NOT NULL DEFAULT '[]',
  proposal_id       INTEGER NOT NULL REFERENCES review_rule_proposal(id),
  approved_by       TEXT NOT NULL,
  approval_event_id INTEGER NOT NULL REFERENCES approval_event(id),
  supersedes        TEXT,
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

CREATE TABLE IF NOT EXISTS fix_branch (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id    INTEGER REFERENCES finding(id),
  workorder_ref TEXT,
  repo_path     TEXT NOT NULL,
  branch_name   TEXT NOT NULL,
  base_sha      TEXT NOT NULL,
  head_sha      TEXT NOT NULL,
  patch_digest  TEXT,
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
  log_digest    TEXT,
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

CREATE TABLE IF NOT EXISTS fix_branch_ship (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  fix_branch_id      INTEGER NOT NULL REFERENCES fix_branch(id),
  workflow_id        TEXT NOT NULL,
  tested_sha         TEXT NOT NULL,
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

CREATE TABLE IF NOT EXISTS branch_authoring (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  fix_branch_id INTEGER NOT NULL REFERENCES fix_branch(id),
  backend       TEXT NOT NULL CHECK (backend IN ('fixture','command')),
  workorder_ref TEXT,
  base_sha      TEXT NOT NULL,
  head_sha      TEXT NOT NULL,
  patch_digest  TEXT NOT NULL,
  provenance    TEXT,
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

CREATE TABLE IF NOT EXISTS pipeline (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  name              TEXT    NOT NULL,
  version           INTEGER NOT NULL,
  definition        TEXT    NOT NULL,
  definition_digest TEXT    NOT NULL,
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

CREATE TABLE IF NOT EXISTS judgement_revision (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  pending_id         INTEGER NOT NULL REFERENCES pending_judgement(id),
  previous_rating_id TEXT    NOT NULL,
  new_rating_id      TEXT    NOT NULL,
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
