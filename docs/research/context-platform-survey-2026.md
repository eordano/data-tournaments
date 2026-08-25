# Project Landscape / Multi-Source Context Platforms — Survey (Aug 2026)

Research for: turning `data-tournaments` into a "project landscape" — multiple data
sources (git repos, issues/PRs, Slack, docs, CI, DBs, APIs, MCP servers) assembled into
reusable context/workflows for agents doing **judging, creation, and sandbox runs**.

Existing assets: Phoenix LiveView control plane, Python DSPy pipeline, domains
(corpus + prompts), Langfuse prompt management, WorkOrder schema with provenance
(base commits, models), MCP server for the tournament DB.

---

## 1. Backstage Software Catalog (Spotify / CNCF)

- Docs: https://backstage.io/docs/features/software-catalog/system-model
- What it is: a developer portal whose core is a **catalog of YAML-declared entities**
  with a well-thought-out ontology:
  - **Component** — a piece of software (service, site, pipeline), tracked in source control.
  - **API** — first-class boundary between components; machine-readable spec (OpenAPI,
    GraphQL, Avro…); visibility: public/restricted/private.
  - **Resource** — infra a component needs at runtime (DBs, buckets, topics).
  - **System** — a handful of components + resources exposing public APIs, hiding
    internals (encapsulation boundary).
  - **Domain** — bounded context grouping systems that share terminology, models, docs.
  - **User/Group** for ownership; **Location** (pointer to more catalog data);
    **Template** (scaffolder: parameters + steps).
- Entities are declared in `catalog-info.yaml` files in repos and continuously ingested;
  relations (`ownedBy`, `partOf`, `dependsOn`, `providesApi`, `consumesApi`) form a graph.
- Self-host: fully open source (Node/React monolith + plugins), but famously heavy to
  adopt — it's a platform product needing a dedicated maintainer; plugin ecosystem is the
  value and the burden.
- **Verdict: borrow the model, not the product.** For a 2-person team the entity
  ontology (Domain → System → Component/Resource/API + owned-by + depends-on + Location
  pointers) is exactly the right shape for a "project landscape" schema in Postgres/Ecto.
  Notably, your existing "domains" concept maps 1:1 to Backstage Domains. Running
  Backstage itself to serve agents would be all cost, no fit (it's a human portal, not a
  context-assembly API).

## 2. Dify

- https://dify.ai · docs: https://docs.dify.ai/en/introduction ·
  self-host: https://docs.dify.ai/en/getting-started/install-self-hosted/readme
- Open-source (150K+ GitHub stars) platform for agents, agentic workflows, chatbots;
  visual canvas; publish as web app or REST API; `difyctl` CLI; plugin marketplace.
- **Knowledge Pipeline** (https://dify.ai/rag): visual, traceable pipelines turning
  files/drives/online docs/web into knowledge bases with structure, metadata, citations.
  RAG v2 shipped through 2025–26; MCP support both directions (consume MCP servers as
  tools; expose apps over MCP).
- Self-host: Docker Compose, ~16 containers (api, workers, plugin daemon, agent backend,
  weaviate, postgres, redis, nginx, dual sandboxes…). Community Edition is real but the
  footprint is substantial for 2 people.
- **Verdict: could technically host the "context assembly" layer** (knowledge pipelines +
  agent workflows + MCP), but it's an app-builder platform, not a project catalog. It has
  no notion of provenance-carrying WorkOrders, corpora at pinned commits, or tournament
  semantics. You'd be embedding your domain inside someone else's abstraction. Inspiration:
  its visual, debuggable, **reusable source→context pipeline** idea is the part worth
  copying.

## 3. Dust.tt

- https://dust.tt · docs: https://docs.dust.tt (connections:
  https://docs.dust.tt/docs/connections)
- SaaS "multiplayer AI" agent platform. **Connections** = fully managed, auto-synced
  integrations (Slack, Notion, GitHub, Google Drive, Confluence, Intercom, Snowflake,
  BigQuery, Zendesk, Gong, Salesforce, Microsoft). Two-step model: (1) ingest via
  connection, (2) expose as Company Data or scoped **Spaces** (permission boundaries).
  Agents get retrieval over selected spaces; MCP support for external tools.
- Self-host: repo is public (https://github.com/dust-tt/dust) but it is **not a
  supported self-host product** — it's their SaaS codebase. Per-seat pricing (~$29+/seat).
- **Verdict: inspiration only.** The Spaces model (curated, permissioned bundles of
  synced sources handed to agents) is the cleanest articulation of "context as a managed
  asset," but it's a closed SaaS aimed at company knowledge work, not a layer you can
  embed under a Phoenix control plane or point at pinned code corpora.

## 4. n8n

- https://n8n.io · AI docs: https://docs.n8n.io/advanced-ai/ ·
  hosting: https://docs.n8n.io/hosting/
- Workflow automation with 400+ connectors plus LangChain-based AI nodes (agents, tools,
  memory, multi-model workflows, evaluations). Native MCP client & server trigger nodes.
- Self-host: excellent — one-line installer, Docker Compose, k8s; fair-code license
  (free Community edition; Business/Enterprise via license key). n8n 3.0 deprecates npm
  install path.
- **Verdict: a plumbing layer, not a catalog.** Great for event-driven sync jobs
  ("on GitHub PR merge → refresh corpus → notify Phoenix"), but it has no data model for
  projects/context and its AI-agent story is workflow-shaped, not context-shaped. Could be
  a pragmatic sidecar for connector breadth if you ever need Slack/Jira ingestion without
  writing clients. Overlaps heavily with what Oban + a few API clients already give you
  in Elixir.

## 5. Newer "context engineering platform" space (2025–2026)

The label covers ~4 distinct layers (good taxonomy: DataHub,
https://datahub.com/blog/context-management-tools/ and Atlan,
https://atlan.com/know/context-engineering-platforms-comparison/):
frameworks (LangChain/LangGraph), **memory layers** (Mem0, Zep, Letta), retrieval
infrastructure, and enterprise context/metadata platforms (Atlan, DataHub — data-catalog
vendors rebranding as "context for agents", MCP servers over governed metadata).

Most relevant new entrant:

- **Airweave** (YC 2025) — https://github.com/airweave-ai/airweave ·
  https://docs.airweave.ai/welcome — "open-source context retrieval layer for agents."
  Connects 50+ apps/DBs/doc sources, continuously syncs, entity-processes, indexes, and
  exposes **one unified search interface via REST/SDKs/CLI/MCP**. MIT license.
  Self-host: `git clone && ./start.sh` (Docker Compose; FastAPI + Postgres + Vespa +
  Temporal + Redis; k8s for prod). This is the closest thing to a drop-in
  "many sources → one agent-queryable context layer" and it speaks MCP natively.
  Caveats: young (2025 company, 7 people), retrieval-only (no catalog/ontology, no
  workflow/provenance), and its infra footprint (Vespa + Temporal) is non-trivial.
- Memory layers (Mem0, Zep, Letta) solve *conversation/agent memory*, not project
  landscape — orthogonal to this need.
- Anthropic's context-engineering guidance (curation, progressive disclosure,
  just-in-time retrieval): https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents

## 6. Standards — current state (Aug 2026)

- **MCP** — spec version **2026-07-28** (https://modelcontextprotocol.io/specification/latest).
  Core: JSON-RPC; servers expose **Resources** (context/data), **Prompts** (templated
  workflows), **Tools** (functions); clients can offer **Elicitation**. Since 2025-06-18:
  structured tool output, OAuth resource-server classification + RFC 8707, resource links
  in tool results. New in 2026: official **Extensions** framework
  (https://modelcontextprotocol.io/extensions) — **Tasks** (async long-running ops with
  durable handles — directly relevant to sandbox runs), **Skills over MCP** (serve Agent
  Skills through MCP), **MCP Apps** (inline interactive UI). Registry + SEP process now
  formalized.
- **Agent Skills** — now an **open standard** with its own site
  (https://agentskills.io, plus https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview).
  A skill = folder with `SKILL.md` (YAML frontmatter + instructions) + optional
  scripts/references/assets; progressive disclosure (metadata → instructions → files);
  adopted across many agent products, not just Claude. This is the natural packaging for
  your "multiple skills" requirement — version-controlled folders agents load on demand.
- **A2A** — https://a2a-protocol.org/latest/ — Linux Foundation project (donated by
  Google; TSC incl. AWS, Microsoft, IBM, Salesforce, SAP). Agent↔agent interop: Agent
  Cards, task delegation, opaque agents; SDKs in Python/JS/Java/C#/Go/Rust. Explicitly
  complementary to MCP (MCP = agent→tool, A2A = agent→agent). Relevant later if judge /
  creator / runner become separately-hosted agents; not needed for a single control plane.

---

## Build-vs-buy recommendation (2-person team, existing Phoenix control plane)

**Build the catalog; standardize the interface; buy/adopt only plumbing.**

None of the surveyed products is a "project landscape + context assembly" layer that
understands what you already have (WorkOrders with provenance, pinned base commits,
Langfuse-managed prompts, tournament DB, judging/creation/sandbox lifecycle). Adopting
Dify or Dust means re-homing your domain model inside an app-builder; Backstage is a
human portal with the right ontology but the wrong runtime; n8n and Airweave are
plumbing layers.

Concretely:

1. **Build a small catalog in Phoenix/Ecto, borrowing Backstage's ontology.**
   Entities: `Domain` (you have this) → `System/Project` → `Source` (git repo @ commit,
   GitHub issues/PRs, docs, CI, DB, API, MCP server — i.e. Backstage's
   Component/Resource/API collapsed into a typed Source with a `Location`-style pointer)
   with relations (`part_of`, `depends_on`, `provides`, ownership) and provenance fields
   you already track on WorkOrders. This is days, not months, and it *is* the product.
2. **Make MCP the assembly interface, not a bespoke API.** You already run an MCP server
   for the tournament DB. Extend it (or add a sibling) so a project's assembled context
   is exposed as MCP **Resources** (corpus slices, docs, WorkOrders), **Prompts**
   (judge/create templates, backed by Langfuse), and **Tools** (queries, sandbox
   launch). Track the **Tasks extension** for long sandbox runs. Any MCP-speaking agent
   (Claude Code, custom DSPy runners) then consumes the landscape for free.
3. **Package procedures as Agent Skills** (`SKILL.md` folders, per the open standard) —
   one per workflow: judging rubric application, WorkOrder creation, sandbox execution.
   Version them in the repo; optionally serve via the Skills-over-MCP extension. This
   answers "multiple skills … whole workflow for agents" with a standard instead of a
   platform.
4. **Adopt narrowly for ingestion if/when needed:** Airweave (MIT, self-hosted, MCP-native)
   if you want unified semantic search across Slack/Notion/GitHub without building sync
   pipelines; or n8n for event-driven connector plumbing. Both slot *under* your catalog
   as sources — neither replaces it. Skip Dify/Dust/Backstage as runtimes.
5. **Defer A2A** until judge/creator/runner are independently deployed agents.

Why not buy: the differentiated 20% (provenance-carrying WorkOrders, tournament
semantics, corpus pinning, DSPy optimization loop) is precisely what no platform sells,
and the undifferentiated 80% (connectors, retrieval) is available as adoptable open
pieces (Airweave, MCP servers from the official registry) without ceding the data model.
For two people, operating Dify's 16 containers or Backstage's plugin treadmill costs
more than writing ~5 Ecto schemas and extending an MCP server you already own.
