---
name: create-workorders
description: Generate provenance-stamped WorkOrders from a creator-role ContextPack for a domain. Use when a project needs new judgeable engineering work items.
version: 0.1.0
---

# Create work orders

Turn a creator-role ContextPack into typed `WorkOrder` drafts via the
generation pipeline. The model supplies judgment (title, goal, plan,
priority + rationale, evidence, files, acceptance criteria, risks); the
system stamps provenance (domain, date, models, base commits, links).

## Required evidence
- Creator-role pack digest from `assemble-project-context`.
- Domain exists with a production generator prompt
  (`card-generator:<domain>` in the prompt store).
- For code work: at least one git source with a pinned base commit.

## Capabilities (allowlist)
- `generate_workorders`
- Resource reads: `landscape://packs/{digest}`, `landscape://evidence/*`

## Procedure
1. Verify the pack role is `creator` and its digest resolves.
2. Call `generate_workorders(pack_digest, domain, budget)`. Respect the
   budget — never fan out unbounded (GENERATOR_MAX_ITEMS semantics).
3. Quality bar per work order: specific title; goal states outcome + why;
   plan is numbered and implementable; evidence cites pack EvidenceRefs
   (not memory); priority has a rationale; acceptance criteria testable.
4. Never fill requester, reviewers, links, commits, or dates — the system
   stamps or a human supplies them. Emitting them is a defect.
5. Return the enqueued pair count and per-item failure breakdown
   (timeout / parse-error / truncation classes).

## Approval boundaries
None — creation only enqueues candidates for judging; nothing executes.

## Outputs
- WorkOrders enqueued as judging pairs; generation report (counts,
  failure classes, aborted_reason if the provider broke).

## Failure handling
- Provider-level failures (auth, connectivity): the pipeline's circuit
  breaker aborts after 3 consecutive; report the abort reason verbatim.
- Truncated/malformed items are failed by class, never repaired into
  valid-looking work orders. Report the breakdown honestly.
