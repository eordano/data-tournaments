# Unity-Explorer Release Platform — Workflow Plan

Workflow ID: `unity-explorer-release-platform-v1`
Started: 2026-08-17 · Status: Phase 0 in progress

Vision (user): "a project landscape with multiple sources of data, multiple
skills, multiple files and apis and mcps, like generation of a whole workflow
for agents that we can use for both judging, creation, and actually also
running on sandboxes" — used to fully manage deployment of new versions of
unity-explorer.

Research basis: docs/research/*.md (Aug 2026 surveys, all claims cited).
Verdict: build the thin differentiated layer (catalog, packs, tournament
semantics), adopt open pieces (Temporal, MCP, Agent Skills, E2B/microvm.nix),
buy nothing that would swallow the domain model.

## Architecture

    ProjectCatalog -> LandscapeSnapshot -> ContextPack (role-shaped, immutable)
      -> WorkOrder / WorkflowSpec (typed, judgeable — tournament reused)
      -> Temporal WorkflowRun (human approval Signals from LiveView)
      -> Sandbox execution (E2B pilot -> microvm.nix Linux runners)
    MCP = assembly interface (Resources/Prompts/Tools)
    Agent Skills = versioned procedures (SKILL.md folders)

## Phases

### Phase 0 — Architecture + contracts (WAVE 1, landed)
- [x] 0a. Storage ADRs — docs/adr/0001 (catalog in judgements.db, Python
      schema owner, workflow_run as projection) + 0002 (hybrid CAS,
      64KiB inline threshold, immutability triggers)
- [x] 0b. Typed context contracts — bin/landscape/ (evidence, snapshot,
      pack, workflow_spec, canonical digests; 57 tests)
- [x] 0c. Markdown sanitization — TournamentUiWeb.SafeMarkdown
      (allowlist Earmark-AST filter, both call sites + link chips;
      21 tests)

### Wave 2A (landed except spike integration)
- [x] 2a-1. Temporal release-workflow spike — spikes/temporal-unity-release/:
      deterministic UnityReleaseWorkflow (approval Signal, durable timers,
      per-stage retry policies, workflow_id release:<repo>:<commit>);
      4/4 tests verified against a real dev server (approve→promoted,
      timeout→rolled_back, reject→rolled_back, activity retry→failure).
      Spike only: PydanticAI TemporalDurability, workflow_run projection,
      and Phoenix approval Signals remain Wave 4.
- [x] 2a-2. Agent Skills package (skills/): assemble-project-context,
      create-workorders, judge-workorders, execute-workorder,
      release-unity-explorer — evidence requirements, capability
      allowlists, approval boundaries per skill (cb947bf)
- [x] 2a-3. Landscape MCP v1 contract (docs/specs/landscape-mcp-v1.md):
      Resources/Prompts/Tools, trust-tier + secret rules (cb947bf)

### Phase 1 — Project catalog ✅
- [x] Persistence per ADR 0001: catalog tables + immutable digest-keyed
      tables + CAS + busy_timeout hygiene (5a1d96f)
- [x] Catalog UI (ea333fc) + MCP Resources via landscape server (5f19cf5)

### Phase 2 — Context assembly ✅
- [x] Source adapters: git_local + github_api (dc0399c), unity_cloud
      (a265f53)
- [x] EvidenceRef normalization + role-shaped ContextPacks: contracts
      (2bbea3c) + assemble_pack pipeline (8ade671)
- [x] Judge view shows cited evidence (ea333fc; WorkOrder digest stamping
      rides the next generation batch)

### Phase 3 — Durable execution (Temporal) ✅
- [x] Spike verified on real dev server (b9585be)
- [x] Production package + workflow_run projection (d84253d, 48033aa,
      8a47a8c coroutine-delivery fix caught in dry-run)
- [x] Runs UI + approval buttons -> audited Signal path (300d48f)

### Phase 4 — Sandboxes ✅ (config/scaffold; runs gated)
- [x] Profiles/backends/preflight-evidence scaffold (604c907); E2B
      backend env-gated (809fddd) — live runs need E2B_API_KEY
- [x] microvm.nix guest + nftables deny-by-default egress (a6f0f7e) —
      execution needs a Linux/KVM host

### Phase 5 — unity-explorer release pilot ✅ (dry-run 2026-08-17)
- [x] Catalog seeded: git + github-releases + unity-cloud-build sources,
      release-approvals policy
- [x] End-to-end PROMOTE path on real Temporal: all 9 stages ok,
      audited human approval (event #3), projection status=done
- [x] End-to-end ROLLBACK path: audited rejection (event #5) ->
      rolled-back, sticky terminal projection
- [x] Backup -> restore drill (sha-verified; runs + audit read back
      intact) + cas-verify clean
- Real build/canary/promote remain documented stubs pending Unity Cloud
  credentials (activities.py states each integration point)

**STATUS: COMPLETE at dry-run level (2026-08-17).** Credential/hardware-
gated remainder tracked in docs/runbook.md §Credential-gated.

## Invariants (non-negotiable, from research)
- Temporal workflows: no direct network/git/LLM/fs — Activities only
- ContextPacks immutable + content-addressed; different projections per role
- Tier-3 (external) evidence never selects tools or approves anything
- Models cannot stamp provenance/links/commits/requester/reviewers/approvals
- Every write/deploy step behind an explicit approval policy
- Execution on Linux sandboxes; Macs are control plane only
- Secrets scoped per Activity; never persisted in WorkOrders or packs

## Verification
Full pytest + mix precommit after every step; one commit per verified step.
No network or real LM calls in tests.
