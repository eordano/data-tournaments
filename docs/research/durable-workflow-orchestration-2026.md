# Durable Workflow + Agent Orchestration Options (researched Aug 2026)

Context: data-tournaments — Phoenix LiveView UI + Python (DSPy, pydantic) pipeline generating/judging WorkOrders, growing toward fully managed Unity game deployments with human approval gates. Team: 1–2 people, Nix, self-hosted, pydantic-typed artifacts everywhere.

All claims verified against live official docs in August 2026. Temporal docs support `.md` suffix for raw markdown (https://docs.temporal.io/llms.txt); ai.pydantic.dev and adk.dev likewise.

---

## 1. Temporal

**What it provides today**
- Durable execution via event-history replay: deterministic *workflows* + I/O-bearing *activities*; workflows survive crashes/restarts and resume exactly where they left off (https://docs.temporal.io/evaluate/understanding-temporal, architecture: https://docs.temporal.io/encyclopedia/architecture/how-temporal-works.md).
- **Signals, Queries, and Updates** for message passing into running workflows — the canonical human-approval mechanism (https://docs.temporal.io/encyclopedia/workflow-message-passing.md, handling: https://docs.temporal.io/handling-messages.md, sending: https://docs.temporal.io/sending-messages.md).
- **Durable timers / start delays** that survive process death — approval timeouts that wait days at zero compute cost (https://docs.temporal.io/workflow-execution/timers-delays.md).
- **AI Cookbook** — first-class AI/agent guidance including a dedicated *Human-in-the-loop AI agent* recipe using Signals for approval with durable timeout + audit trail (https://docs.temporal.io/ai-cookbook, HITL recipe: https://docs.temporal.io/ai-cookbook/human-in-the-loop-python.md). Other recipes: durable agentic loops (Claude/OpenAI tool calling), durable MCP servers, OpenAI Agents SDK integration, claim-check pattern for large payloads, guardrails.
- Python SDK is mature (https://python.temporal.io/); `temporalio/contrib/openai_agents` exists in the Python SDK (https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents).

**Self-hosting burden**
- Real but manageable: Temporal Server (Go) + Postgres/MySQL/SQLite + optional Elasticsearch for advanced visibility. Official self-hosted guide covers Docker/K8s/manual deployment, security, monitoring, upgrades (https://docs.temporal.io/self-hosted-guide, deployment: https://docs.temporal.io/self-hosted-guide/deployment.md, production checklist: https://docs.temporal.io/self-hosted-guide/production-checklist.md).
- Dev mode is a single binary: `temporal server start-dev` (https://docs.temporal.io/self-hosted-guide.md). **Both `temporal` (server) and `temporal-cli` are packaged in nixpkgs** (verified: pkgs/by-name/te/temporal/package.nix and te/temporal-cli/package.nix on nixpkgs master) — a NixOS module/systemd unit around the server + Postgres is a small amount of work.
- Postgres-only visibility is fine at 1–2-person scale; Elasticsearch optional (https://docs.temporal.io/self-hosted-guide/visibility.md).

**Python + Elixir interop**
- No Elixir SDK. Official SDKs: Go, Java, Python, TS, .NET, PHP, Ruby, Rust (https://docs.temporal.io/references/api-reference.md). Server speaks gRPC (Server Frontend API: https://docs.temporal.io/self-hosted-guide/server-frontend-api-reference.md).
- Phoenix options: (a) call the gRPC frontend from Elixir with grpc-elixir + generated protos to start workflows / send signals / query state — workable for the thin "start + signal + query" client surface even without a full SDK (workers stay in Python); (b) simpler: a small FastAPI/Python sidecar that Phoenix calls over HTTP, or Phoenix reads workflow state that Python workers project into Postgres/PubSub. The heavy SDK machinery (workers, replay) is only needed on the Python side.

**Fit for release pipelines with approval gates**
- Best-in-class. The exact pattern (LLM proposes action → risky actions pause on Signal → approve/reject/timeout → execute) is a documented recipe (https://docs.temporal.io/ai-cookbook/human-in-the-loop-python.md). Deployment pipelines with retries, compensation, long waits, and full audit history are Temporal's home turf.

---

## 2. LangGraph

**What it provides today** (docs moved to docs.langchain.com)
- **Persistence**: checkpointers persist per-thread graph state snapshots (conversation continuity, HITL, time travel, fault tolerance); stores persist cross-thread data. Production checkpointers: `PostgresSaver`/`AsyncPostgresSaver`, `SqliteSaver` (https://docs.langchain.com/oss/python/langgraph/persistence, https://docs.langchain.com/oss/python/langgraph/checkpointers).
- **Interrupts**: `interrupt()` pauses a node, saves state via the checkpointer, waits indefinitely; resume with `Command(resume=...)` on the same `thread_id`. Documented approval-workflow, review-and-edit, and tool-call-interception patterns (https://docs.langchain.com/oss/python/langgraph/interrupts).
- Caveat documented on the same page: on resume, **the node restarts from its beginning** — code before `interrupt()` re-runs, so side effects in that span need idempotence. Durability is state-snapshot-based, not replay-of-history; a crash mid-node loses work back to the last checkpoint boundary.

**Self-hosting burden**
- Minimal: it's a library + your Postgres. No server component required for OSS usage (the hosted "Agent Server"/LangSmith platform handles persistence automatically but is optional/commercial: https://docs.langchain.com/oss/python/langgraph/persistence).

**Python + Elixir interop**
- Library-only, in-process Python. Phoenix must call a Python service you write (HTTP/WebSocket) that invokes/resumes graphs. No protocol layer given for free; you own the API, queueing, worker lifecycle, and crash recovery of the Python process itself.

**Fit for deployment pipelines**
- Good for the agent/LLM-graph layer; weaker as a *pipeline engine*: no durable timers, no signal semantics, no built-in retries/schedules/workers — you rebuild those around it. Ties you to the LangChain ecosystem while your stack is DSPy + pydantic.

---

## 3. PydanticAI durable execution (verified at ai.pydantic.dev)

PydanticAI now has a first-class durable-execution section with **three native integrations** — Temporal, DBOS, and Prefect (https://ai.pydantic.dev/durable_execution/overview/):

- **Temporal** (https://ai.pydantic.dev/durable_execution/temporal/): `pip install pydantic-ai[temporal]`. Attach `TemporalDurability()` as an agent *capability*; inside a Temporal workflow, model requests, tool calls, and MCP communication are automatically routed through activities; outside a workflow the agent behaves normally. `PydanticAIPlugin` makes Temporal use pydantic for (de)serialization and registers agent activities; sensible non-retryable error classification built in. This is the deepest integration of the three.
- **DBOS** (https://ai.pydantic.dev/durable_execution/dbos/): `pip install pydantic-ai[dbos]` + `DBOSDurability()` capability. DBOS is a **library, no server** — checkpoints workflow/step state into Postgres (SQLite for dev); on restart, workflows resume from last completed step. Durable queues replace Celery. Lightest operational footprint of any option here.
- **Prefect** (https://ai.pydantic.dev/durable_execution/prefect/): `pip install pydantic-ai[prefect]` + `PrefectDurability()` capability; model/tool/MCP calls become Prefect tasks with transactional/idempotent semantics (Prefect 3.0). Client-side orchestration; optional Prefect server for scheduling/UI. More of a data-pipeline tool; HITL approval gates are not its strength.

Key point: **your existing pydantic-everywhere investment carries straight through** — with Temporal the payloads on the wire are pydantic models, and agent code is unchanged inside vs. outside a workflow.

---

## 4. Brief: OpenAI Agents SDK, Microsoft Agent Framework, Google ADK

- **OpenAI Agents SDK (Python)**: has a dedicated human-in-the-loop guide (tool `needs_approval`, run interruptions, approve/reject, serialize state and resume later — even after process restart) (https://openai.github.io/openai-agents-python/human_in_the_loop/). For *durability* it explicitly points at external engines: Temporal integration ("Durable execution integrations and human-in-the-loop", https://openai.github.io/openai-agents-python/running_agents/; Temporal contrib module: https://github.com/temporalio/sdk-python/tree/main/temporalio/contrib/openai_agents) and Restate (https://www.restate.dev/blog/durable-orchestration-for-ai-agents-with-restate-and-openai-sdk). Sessions (e.g. `SQLiteSession`) persist conversation history, not workflow durability (https://openai.github.io/openai-agents-python/sessions/). Not a workflow engine on its own.
- **Microsoft Agent Framework**: Workflows have **checkpointing** — superstep-boundary checkpoints via `CheckpointManager`, resume from any saved checkpoint index; docs warn checkpoint storage is a trust boundary (https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints), plus a human-in-the-loop workflow capability (https://learn.microsoft.com/en-us/agent-framework/workflows/human-in-the-loop). .NET-first with Python; Azure-leaning; no Elixir story; heavier conceptual surface than a 2-person team needs.
- **Google ADK**: `ResumabilityConfig(is_resumable=True)` gives resume-after-interruption (Python ≥1.16) (https://adk.dev/runtime/resume/index.md); graph workflows support HITL `RequestInput` nodes (https://adk.dev/graphs/human-input/index.md); true durable execution is delegated to the **Restate plugin** (journaled LLM/tool calls, pause/resume for approvals for days/weeks) (https://adk.dev/integrations/restate/index.md). Session persistence via pluggable services (e.g. Firestore: https://adk.dev/integrations/firestore-session-service/index.md). Ecosystem gravity is Google Cloud (Cloud Run/GKE/Agent Runtime deploy targets: https://adk.dev/deploy/index.md).

---

## Comparison table

| | Temporal (+PydanticAI) | DBOS (+PydanticAI) | LangGraph | OpenAI SDK / MSAF / ADK |
|---|---|---|---|---|
| Durability model | Event-history replay; strongest guarantees | DB checkpoint per step; resume from last step | State snapshot per superstep; node re-runs from start | Varies; real durability delegated (Temporal/Restate) or checkpoint-lite |
| Approval gates | Signals + durable timers, documented HITL recipe | `DBOS.recv`/events + queues (build it yourself) | `interrupt()`/`Command(resume=)`, well documented | Supported but tied to each SDK's runtime |
| Self-host cost | Server + Postgres (+optional ES); in nixpkgs | **None — library + Postgres** | None (library) but you build the service layer | MSAF/ADK pull toward Azure/GCP |
| Elixir interop | gRPC frontend API callable from Elixir; workers in Python | None — Phoenix→Python HTTP sidecar | None — Phoenix→Python HTTP sidecar | None meaningful |
| Pydantic fit | Native (`pydantic-ai[temporal]`, pydantic serialization) | Native (`pydantic-ai[dbos]`) | LangChain-flavored | OpenAI SDK is pydantic-based; others mixed |
| Ops observability | First-class Web UI, full event history audit | Postgres tables + DBOS console | LangSmith (commercial) or DIY | Varies |

---

## Ranked recommendation

**1. Temporal, self-hosted, with the PydanticAI `TemporalDurability` integration.**
It is the only option that natively covers every requirement at once: durable execution that survives restarts (replay), human approval gates as a documented first-class pattern (Signals + durable timers + audit history: https://docs.temporal.io/ai-cookbook/human-in-the-loop-python.md), sandboxed agent execution as activities with retry policies, pydantic models end-to-end (https://ai.pydantic.dev/durable_execution/temporal/), and packaged in nixpkgs for reproducible self-hosting (dev: `temporal server start-dev`; prod: https://docs.temporal.io/self-hosted-guide). Deployment orchestration of unity-explorer (build → test → judge gate → human approval → release → rollback/compensation) maps 1:1 onto workflow/activity/signal primitives. Phoenix LiveView talks to it via the gRPC frontend from Elixir or a thin Python API service; approval buttons in LiveView become `SignalWorkflow` calls. Cost: one more service (server + Postgres) and the workflow-determinism discipline — acceptable given the deployment-pipeline ambitions.

**2. DBOS via `pydantic-ai[dbos]` — if you refuse to run any new server.**
Library-only durability checkpointed into Postgres you already run; workflows resume from the last completed step after crashes (https://ai.pydantic.dev/durable_execution/dbos/). Great Nix story (pure Python dep). You give up Temporal's signal/timer ergonomics, Web UI, and the documented HITL recipe — approval gates are DIY (DBOS events/queues + your own LiveView plumbing). A very reasonable starting point that shares the same PydanticAI capability API, so **migrating DBOS→Temporal later is nearly a one-line change per agent** (`DBOSDurability()` → `TemporalDurability()` plus workflow scaffolding).

**3. LangGraph** — best-documented `interrupt()` HITL UX (https://docs.langchain.com/oss/python/langgraph/interrupts) and only needs Postgres, but it's an agent-graph library, not a pipeline engine: no durable timers/signals/schedulers, node re-execution semantics on resume, and it drags in the LangChain ecosystem alongside your DSPy+pydantic stack. Choose only if the agent-graph model itself is the product.

**4. OpenAI Agents SDK** — fine as an *agent layer on top of* Temporal (official contrib integration), not as the orchestration substrate. **5. Google ADK / 6. Microsoft Agent Framework** — checkpoint/resume exists (https://adk.dev/runtime/resume/index.md, https://learn.microsoft.com/en-us/agent-framework/workflows/checkpoints) but both pull toward their clouds, add a second agent framework you don't need, and have no Elixir or Nix story; ADK's own answer for real durability is "add Restate," which concedes the point.

**Concrete path**: keep DSPy/pydantic generation+judging as-is → add `pydantic-ai[temporal]` workers for WorkOrder execution → model each WorkOrder execution as one workflow (`workflow_id = work_order_id` gives idempotence + provenance) → approval gates = LiveView button → Elixir gRPC (or Python sidecar) → Signal → later add the unity-explorer release pipeline as a parent workflow with child workflows per stage.
