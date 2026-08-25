# ADR 0002 — Content-addressed artifact storage (snapshots, packs, evidence bodies)

- Status: Proposed
- Date: 2026-08-17
- Depends on: ADR 0001 (catalog lives in `judgements.db`, Python owns schema)
- Relates to: `docs/plans/unity-explorer-release-platform.md` lines 33–36
  (Phase 0b: content-addressed digests, immutable serialization) and line 72
  (invariant: "ContextPacks immutable + content-addressed").

## Context

ADR 0001 puts digest-keyed rows (`evidence_ref`, `landscape_snapshot`,
`context_pack`, `workflow_spec`) in the fabric SQLite DB. Two open questions
remain: (1) where large payloads live, and (2) what exactly gets hashed.

Constraints observed in the repo:

- The fabric DB already stores one unbounded-growth text column —
  `optimizer_run.log`, appended line-by-line (`bin/optimizer_runs.py:41,81–87`).
  That is tolerable for logs but a warning sign: SQLite rows read whole; a
  LiveView listing snapshots must not drag megabytes of manifest per row.
- Evidence bodies can be arbitrarily large (diffs, build logs, release notes)
  and Phase 4 sandbox reports become EvidenceRefs too (plan lines 60–61).
- The repo already treats the filesystem under `$DATA_TOURNAMENTS_HOME` as
  first-class storage: prompts (`bin/prompts.py:5,43`), optimizer artifacts
  (`README.md:258`, `$DATA_TOURNAMENTS_HOME/optimizer/`), uploads/runs/sessions
  (`ui/lib/tournament_ui/paths.ex:23–25`).

## Decision

**Hybrid: metadata + small bodies in SQLite; large payloads in a filesystem
CAS under `$DATA_TOURNAMENTS_HOME/cas/`.**

1. **Digest definition (Phase 0b owns this).** `digest = "sha256:" + hex` over
   the *canonical serialization* produced by the Phase 0b Python contracts
   (sorted keys, no insignificant whitespace, `schema_version` included). The
   DB never computes digests; only the Python builders do. Elixir treats
   digests as opaque keys.
2. **Inline threshold.** If the canonical body is ≤ 64 KiB it is stored inline
   in the row (`evidence_ref.body`, `*.manifest`). Larger bodies are written
   to the CAS and the row column is `NULL`; readers resolve
   `cas_path(digest)`. One threshold, enforced by the Python writer, so a row
   is always self-describing (`body IS NULL` ⇒ look in CAS).
3. **CAS layout.** `$DATA_TOURNAMENTS_HOME/cas/sha256/<first-2-hex>/<hex>`
   (fan-out dir to keep directory listings sane). Files are written to a temp
   name and `rename(2)`d into place — atomic on the same filesystem, and a
   re-write of an existing digest is a no-op by definition.
4. **Immutability enforcement.**
   - DB: `BEFORE UPDATE … RAISE(ABORT)` triggers per digest-keyed table
     (sketched in ADR 0001 §3). DELETE is allowed only via explicit GC
     tooling, never application code.
   - CAS: files are chmod `0444` after rename.
5. **Coexistence with mutable rows.** Immutable rows may hold integer FKs to
   mutable catalog rows (e.g. `evidence_ref.source_id`) *plus* copies of any
   attribute whose at-capture value matters (e.g. `trust_tier`). Rationale:
   the FK answers "which source is this, as it exists now"; the copied column
   answers "what did we believe when we captured it". Mutable rows never
   reference immutable rows by anything other than digest.
6. **GC (deferred).** No automatic garbage collection in Phase 1–2. When
   needed: a Python CLI that deletes CAS files (and rows) unreachable from any
   snapshot/pack/workflow_spec referenced within a retention window. Do not
   build until disk pressure is real.

## Rejected alternatives

- **Everything inline in SQLite.** Simplest, and SQLite handles blobs fine,
  but conflates "list the catalog" reads with multi-MB payload reads, bloats
  `judgements.db` (backed up / copied as one file), and makes the DB the
  bottleneck for what is fundamentally write-once static content.
- **Everything in CAS, DB stores only digests.** Clean, but forces a file
  read (and JSON parse) to display even a one-line evidence summary; the
  queryable columns (`kind`, `trust_tier`, `role`, `snapshot_digest`) would
  have to be duplicated into the DB anyway, which is exactly the hybrid.
- **Git as the CAS** (commit packs/snapshots into a repo). Attractive audit
  story, but adds a second writer discipline (index locking), requires git on
  every reader path, and content-addressing via git object ids would leak git
  canonicalization into the Phase 0b digest contract.
- **Object store (S3/minio).** A service to run; nothing here is multi-host
  yet (ADR 0001 §2 revisit triggers cover the multi-host future).

## Consequences

- Easier: fabric DB stays small and fast; snapshots/packs are trivially
  rsync-able artifacts; digest verification is `sha256sum` away; immutability
  is mechanically enforced at both layers.
- Harder: two storage locations to keep consistent — mitigated by write
  ordering (CAS file first, then DB row; a CAS file without a row is harmless
  garbage, a row without its CAS file is a hard error surfaced by readers) —
  and backup must include both `judgements.db` and `cas/` (they already live
  under the same `$DATA_TOURNAMENTS_HOME` root).
