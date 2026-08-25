---
name: assemble-project-context
description: Build an immutable, role-shaped ContextPack for a project objective from catalog sources. Use before any creation, judging, or execution task that needs project context.
version: 0.1.0
---

# Assemble project context

Produce a `ContextPack` (immutable, content-addressed, role-shaped) that
downstream skills cite by digest. Never hand an agent loose, unversioned
context.

## Required evidence
- Project exists in the catalog with ≥1 source (`landscape://projects/{id}`).
- For git sources: repo reachable and the target ref resolvable to a commit.

## Capabilities (allowlist)
- `assemble_pack`
- Resource reads: `landscape://projects/*`, `landscape://sources/*`,
  `landscape://evidence/*`

## Procedure
1. Read the project entry; enumerate sources relevant to the stated
   objective. Prefer fewer, higher-trust sources over bulk inclusion.
2. Call `assemble_pack(project_id, role, objective)`. Role selection:
   - `creator` — generation tasks (includes tier-3, labeled)
   - `judge` — pairwise judging (tier-3 flagged UNTRUSTED)
   - `executor` — sandbox/deploy tasks (tier-3 excluded by construction)
3. Verify the returned pack: every EvidenceRef has canonical_uri, revision,
   trust_tier, and (for git evidence) a commit-pinned browsable link.
4. Return the pack digest + a one-paragraph coverage summary: what was
   included, what was deliberately left out, known gaps.

## Approval boundaries
None — assembly is read-only.

## Outputs
- `pack_digest` (primary), `snapshot_digest`, coverage summary.

## Failure handling
- Source unreachable → report which source and stop; never substitute
  remembered or guessed content for missing evidence.
- Empty evidence selection → stop; an empty pack is a configuration error,
  not a valid input to downstream skills.
