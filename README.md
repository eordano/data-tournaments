# data-tournaments

Single-elimination tournaments where each match is a sandboxed LLM agent that
compares two inputs, **picks a winner**, and submits a synthesis. The chosen
artifact (winner content or synthesis) feeds the next round; rounds continue
until one guideline remains.

Tracing, dataset versioning, and LLM-as-judge scoring all run through
[Langfuse](https://langfuse.com) — every match becomes a trace, every round
becomes a dataset run, and three evaluators score each conclusion.

## Layout

```
bin/                 Shell + Python harnesses (no build step)
  _env.sh            Sourced prelude: DATA_HOME, BIN_DIR
  hermes-harness.sh  Hermes-backed harness (read_file + pick_winner MCP tools)
  hermes_mcp_server.py  Stdio MCP server: read_file + pick_winner, traces to Langfuse
  run-tournament.py  Orchestrator: config.json → rounds → matches → Langfuse experiments
  setup-hermes-slots.sh  One-time: registers N Hermes MCP slots
  dt_stack.py        Supervisor for the serving stack: Temporal dev server,
                     release worker, Phoenix UI (`dt_stack.py up|down`; state
                     under $DT_STACK_HOME, default ~/.dt-stack)
  with_lock.py       fcntl.flock helper (re-execs under a lock)
configs/             Example tournament config JSONs
docs/                ADRs, specs, design notes, research surveys, runbook, and
                     click-through showcases with screenshots + run artifacts
infra/               Caddyfile (single-origin proxy), an nginx vhost example
                     for public exposure, and a microvm egress profile
nix/ + packages/     NixOS module and package expressions (tournament UI,
                     dspy, gepa, magicattr)
reports/             End-to-end evaluation reports from real runs
skills/              Agent skill definitions used by the workorder pipeline
spikes/              Exploratory prototypes (e.g. Temporal release workflow)
tests/               pytest suite: harnesses, catalog, landscape adapters
                     (the UI keeps its own ExUnit tests under ui/test/)
flake.nix            Pins langfuse + httpx via python3.withPackages
ui/                  Phoenix LiveView workflow (domains, review, results, prompt tuning, direct brackets)
```

## Environment

| Var | Default | Purpose |
|-----|---------|---------|
| `DATA_TOURNAMENTS_HOME` | `/tmp/data-tournaments` | Runtime state: logs, locks, slot files, uploads |
| `DATA_TOURNAMENTS_BIN`  | `$(realpath bin)`        | Where the scripts live |
| `DATA_TOURNAMENTS_CONFIGS` | `$(repo)/configs`     | Where tournament JSON configs live |
| `TOURNAMENT_BROWSE_ROOTS` | `/Users/user/projects:/tmp` | Server-side file-browser sandbox |
| `LANGFUSE_PUBLIC_KEY` | _(required for tracing)_ | Langfuse API public key |
| `LANGFUSE_SECRET_KEY` | _(required for tracing)_ | Langfuse API secret key |
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Self-hosted: set to your instance URL |
| `PROMPT_BACKEND` | `auto` | `local` uses `${DATA_TOURNAMENTS_HOME}/prompts.json`; `langfuse` uses the API; `auto` selects Langfuse only when credentials exist |
| `OPENROUTER_API_KEY` | _(required for frontier panel)_ | Runs Kimi K3, GLM 5.2, and Claude Opus 5 generation/judging roles |
| `LLM_TIMEOUT_SECONDS` | `90` | Per-request timeout for DSPy judging and optimization roles (generation has its own default, below) |
| `LLM_NUM_RETRIES` | `2` | Retries after an LLM request failure (judging/optimization roles) |
| `GENERATOR_MAX_TOKENS` | `16384` | Output-token ceiling for card generation; sized for reasoning-heavy models whose hidden analysis precedes the parseable payload |
| `GENERATOR_TIMEOUT_SECONDS` | `LLM_TIMEOUT_SECONDS` or `180` | Per-request timeout for card generation (sized so a full 16K generation completes) |
| `GENERATOR_NUM_RETRIES` | `0` | Card generation fails fast; the corpus loop records the failure class and continues |
| `GENERATOR_MAX_ITEMS` | `50` | Per-run corpus item budget when no explicit `--limit` is passed; prevents one-LLM-call-per-file fan-out on large repositories |
| `OPTIMIZER_MIN_VALIDATION` / `OPTIMIZER_MIN_HOLDOUT` | `2` / `2` | Minimum eval-split sizes; below them the optimizer retains the production seed without spending LM budget |
| `TOURNAMENT_HERMES_CMD` | `nix run ~/projects/sandboxed-agents#hermes --` | argv prefix used to launch the Hermes agent. Override if your hermes install lives elsewhere (e.g. `TOURNAMENT_HERMES_CMD="hermes"`). |

The full variable reference (model-role selection, provider precedence,
context budget) lives in the `bin/llm_config.py` module docstring — the
single source of truth all runtime code reads through.

Hermes is a sandboxed agent runtime from a separate project; the
`TOURNAMENT_HERMES_CMD` default assumes its flake is checked out at
`~/projects/sandboxed-agents`. Point the variable at any command that
launches an agent exposing the `read_file` + `pick_winner` MCP tools.

If the Langfuse env vars are unset, the orchestrator and MCP server still run
end-to-end — just without traces, dataset runs, or judge scores. The LLM-judge
evaluator additionally needs whatever API key is named in `judge.api_key_env`
(see "Tournament config" below).

## One-time setup

```bash
nix develop                              # drops into a python+langfuse shell
./bin/setup-hermes-slots.sh 4            # builds the flake's MCP-server binary
                                         # and registers 4 Hermes slots that
                                         # invoke its absolute /nix/store path
cd ui && mix deps.get                    # only if you want the UI
```

Re-run `./bin/setup-hermes-slots.sh N` whenever `flake.nix` changes the pinned
langfuse version — the slot's `command:` field needs to point at the new
store path.

## Run a tournament from the CLI

```bash
nix develop -c ./bin/run-tournament.py configs/actions-hermes.json            # resume
nix develop -c ./bin/run-tournament.py configs/actions-hermes.json --fresh    # from scratch
# or via the flake:
nix run . -- configs/actions-hermes.json
```

Streams progress to stdout; the final winning markdown is printed at the end.
Each round prints its Langfuse dataset/run name so you can jump straight to
the trace UI to see per-match scores.

## Run the UI

```bash
cd ui && nix shell nixpkgs#elixir nixpkgs#erlang -c mix phx.server
# → http://localhost:4000
```

The primary workflow starts at `/`:

1. **Start** — choose one evaluation lens.
2. **Domains** — configure the source, generate candidate pairs, and see lifecycle progress.
3. **Review** — submit human verdicts, optionally scoped to one domain.
4. **Results** — compare candidates, source references, human verdicts, and the model panel by match.
5. **Prompts** — inspect versions and improve the rubric from reviewed examples.

Start separates two cases:

- **Generate from a corpus** — choose one evaluation lens (correctness, security,
  maintainability, tests/observability, conventions, or custom), generate
  focused candidates, then judge every pair with that domain's brief.
- **Compare prepared artifacts** — send existing files/documents directly
  into a bracket with one match prompt.

Keep unlike evaluation categories in separate domains. This makes pairwise
verdicts meaningful and ensures both human and LLM raters use the matching
domain-specific judge prompt.

Code-domain globs may be comma-separated and recursive; dependency, cache,
vendor, and build directories are skipped by default. After seven human
verdicts in a domain, use **Improve rubric** to create a prompt candidate trained
only on that category's decisions.

`/brackets` is the advanced direct-artifact workflow for inputs that are
already comparable. `/inspect` is a raw, read-only diagnostic surface; use
Results for reviewer-facing outcome analysis.

## Reverse proxy: one origin for app + Langfuse REST

Two paths depending on whether you want a public URL or just local dev:

### Local-only (Caddy, no public exposure)

`infra/Caddyfile` puts Caddy in front of Phoenix and proxies the Langfuse
REST API, so a single tunnel CAN expose both — but for pure local dev you
don't need the tunnel:

```bash
# terminal 1 — Phoenix
nix develop --command bash -c 'set -a && . .env && set +a && cd ui && PORT=4777 mix phx.server'

# terminal 2 — Caddy
nix develop --command caddy run --config infra/Caddyfile --adapter caddyfile
# → http://localhost:8080            — Phoenix
# → http://localhost:8080/lf/api/*   — Langfuse REST (path-stripped)
```

Configurable via env (defaults shown):

| var | default | what |
|---|---|---|
| `PROXY_LISTEN` | `:8080` | Caddy bind address |
| `PHOENIX_UPSTREAM` | `localhost:4777` | where `mix phx.server` is bound |
| `LANGFUSE_UPSTREAM` | `langfuse.example` | Langfuse FQDN (HTTPS) |

### Public URL via a TLS-terminating host (preferred when you have one)

If the Phoenix dev server should be reachable from outside the dev
machine at e.g. `https://tournaments.example`, the cleanest path is an
nginx vhost on whichever host already terminates TLS for your domain.
The proxyPass upstream is the dev machine's tailnet IP, so dev still
happens locally — only the public hostname lives on the TLS host.

`infra/colmena/tournaments.nix` is a ready-made NixOS/colmena module for
that vhost; adapt the hostname and upstream to your network.

On the dev machine, run Phoenix with `PHX_LISTEN_ALL=1` so it binds
`0.0.0.0:4000` instead of loopback only:

```bash
nix develop --command bash -c '
  set -a && . .env && set +a
  cd ui && PORT=4000 PHX_LISTEN_ALL=1 mix phx.server
'
```

Tailnet ACLs gate access — public users hit the TLS host's nginx, nginx
proxies to the dev machine over the tailnet, the dev machine responds.

## Tournament config

The `configs/*.json` shape (defaults shown):

```jsonc
{
  "name":              "tournament",                 // also used as Langfuse dataset suffix
  "inputs":            ["path1", "path2"],           // round-1 files
  "parallelism":       0,                            // 0=auto (1, since Hermes is serial per-slot)
  "seed":              20260101,
  "db_path":           "/tmp/<name>.db",
  "workdir":           "/tmp/<name>",
  "match_prompt":      "<template>",                 // {LABEL},{INPUTS},{N_INPUTS}
  "required_sections": ["## ..."],
  "max_words":         500,
  "advance":           "synthesis",                  // "synthesis" | "winner"
  "judge": {
    "model":           "moonshotai/kimi-k3",
    "base_url":        "https://openrouter.ai/api/v1", // any OpenAI-compatible endpoint
    "api_key_env":     "OPENROUTER_API_KEY",
    "temperature":     0.0
  },
  "langfuse": {
    "host":            "https://cloud.langfuse.com",
    "tags":            ["tournament", "actions-style"]
  }
}
```

`advance` — what gets passed to the next round:
- `"synthesis"` (default) — the agent's synthesis markdown advances. Closest to
  the pre-Langfuse behavior; the synthesis already references the winner.
- `"winner"` — the chosen input's raw content advances verbatim. Stricter
  elimination: low-quality syntheses can't survive past the round they're in.

Both modes record the full synthesis + `winner_id` + reasoning into the trace
and the SQLite cache; only the next-round payload differs.

## Match prompt

The `match_prompt` is a template with three placeholders: `{LABEL}`,
`{INPUTS}` (numbered list `1. /path/a.ts\n2. /path/b.ts`), `{N_INPUTS}`. The
agent must call `pick_winner(winner_id, reasoning, markdown)` exactly once and
then stop.

## Evaluators

Every match's trace gets three scores:

| Evaluator | Type | What it checks |
|-----------|------|----------------|
| `required_sections_present` | BOOLEAN | All `required_sections` headings appear in the synthesis |
| `word_count_within_limit` | BOOLEAN | Synthesis ≤ `max_words` |
| `synthesis_quality_judge` | NUMERIC 0–1 | LLM-as-judge: coherence, specificity, defensibility of the chosen winner |

Plus a CATEGORICAL `winner_id` score (`input_1` or `input_2`) on every trace
so you can filter the dataset run by which side won.

The judge calls whatever OpenAI-compatible endpoint is configured under
`judge.*`. The default is `moonshotai/kimi-k3`. The judgement fabric uses a
deliberately selected frontier panel of structured-output-capable models:
Kimi K3, GLM 5.2, and Claude Opus 5.

## Context optimizer

The Prompt studio and each domain's **Improve rubric** action run a conservative
DSPy + GEPA context-evolution pipeline. It requires at least seven human
judgements, removes duplicate card pairs, and deterministically partitions the
remaining examples into train, validation, and untouched holdout sets.

The three frontier models have separate responsibilities:

- Kimi K3 runs the judge and produces decision trajectories.
- GLM 5.2 reflects on rich per-example feedback while GEPA 0.1.4 searches a
  Pareto frontier under an explicit metric-call budget.
- Claude Opus 5 curates GEPA's discoveries into structured, incremental
  playbook entries without replacing the production seed prompt.

The seed and curated candidate are evaluated on the same untouched holdout.
A candidate label is created only when the candidate improves the verdict
metric, does not reduce exact accuracy, and does not increase invalid outputs;
otherwise the run records `plateau` or `regression` and retains production.
Every run saves its configuration, prompt lineage, GEPA checkpoints, paired
outcomes, and decision under `$DATA_TOURNAMENTS_HOME/optimizer/`.

```sh
python bin/optimize.py --domain my-domain \
  --prompt-name judge-instructions:my-domain \
  --max-metric-calls 40 --seed 0
```

The direct bracket page also has a separate, legacy **Optimize prompt** helper.
It uses `claude -p` with the current prompt, three sample conclusions, and a
free-form critique; **Apply to config** writes that rewrite to tournament JSON.

## Input sources (new tournament form)

Four ways to specify inputs, combinable:

1. **Server-side file browser** — navigate folders, tick individual files, or
   use `⊕` to select recursively. Sandbox is `TOURNAMENT_BROWSE_ROOTS`.
2. **Upload** — drag-and-drop from the browser.
3. **Database query** — sqlite:/// or postgres:// URL + a `SELECT name, content FROM …`
   query. Preview shows first 5 rows; "Add to inputs" writes up to 1000 rows
   into `$uploads/<name>/` and selects them.
4. **JS transform** — upload one source file and write a JS function
   `process(input) -> [{name, content}, ...]`. Each returned item becomes
   one tournament input. Requires `node` on PATH.

Both DB and JS sources must yield `{name, content}`; the UI writes each entry
as a file under `$uploads/<tournament-name>/<name>` and adds the path to the
selected-inputs set.
