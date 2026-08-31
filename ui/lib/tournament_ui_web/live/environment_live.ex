defmodule TournamentUiWeb.EnvironmentLive do
  @moduledoc """
  /environment — the operator surface for everything a campaign runs IN
  (contract: docs/design/operator-environment-v13.md §2).

  One LiveView, five tabs via `?tab=`:

    * `sources`   — catalog projects with evidence-source counts (the old
      /catalog index, incl. the inline "New project" form). Project
      drill-down links keep going to /catalog/:project, which stays a live
      detail page (source forms/archive live there).
    * `prompts`   — the old /prompts content: prompt versions/labels with
      inline detail, promote, and the context optimizer.
    * `rubrics`   — eval_template registry: PAIR/SINGLE chip, subject
      badges, wheel presence, verdict-enum summary. Legacy templates with
      no judgement_kind render as PAIR / execution (bin/judgement.py
      output_definition v2 defaulting).
    * `pipelines` — pipeline registry (stage flow + digest) and domain
      bindings.
    * `policies`  — policy names, kind, scope, approver NAMES only. Rule
      bodies are never rendered (no secret values, ever).

  Old routes /catalog and /prompts push_navigate here on mount. Tabs whose
  tables predate this data home render an honest "not initialized" note
  (Environment adapter returns :unavailable) instead of a fake empty list.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.Catalog
  alias TournamentUi.Environment
  alias TournamentUi.Judgement
  alias TournamentUi.LangfusePrompts
  alias TournamentUi.LlmModels
  alias TournamentUi.OptimizerRunner
  alias TournamentUi.OptimizerRuns

  @tabs ~w(sources prompts rubrics pipelines policies)

  @optimizer_script System.get_env("OPTIMIZE_SCRIPT") ||
                      Path.expand("../../../../bin/optimize.py", __DIR__)

  @repo_root_static System.get_env("DATA_TOURNAMENTS_REPO") ||
                      Path.expand("../../../../..", __ENV__.file)

  @blank_project %{"name" => "", "description" => ""}

  # Model-name substrings that mark obviously non-chat models (STT, TTS,
  # embeddings, moderation, image, audio/realtime). Case-insensitive.
  @non_chat_markers ~w(whisper tts embed embedding moderation dall-e audio realtime)

  @doc "Filter a model list down to chat-capable candidates (F-6)."
  def chat_models(models) do
    Enum.reject(models, fn m ->
      down = String.downcase(m)
      Enum.any?(@non_chat_markers, &String.contains?(down, &1))
    end)
  end

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket), do: :timer.send_interval(5_000, :refresh)

    {:ok,
     assign(socket,
       tab: "sources",
       # sources
       projects: [],
       show_project_form: false,
       project_error: nil,
       project_values: @blank_project,
       # prompts
       prompts_loaded: false,
       prompts: [],
       log_lines: [],
       optimizer_status: :idle,
       optimizer_error: nil,
       latest_run: nil,
       prompt_backend: nil,
       default_rubric: Judgement.default_rubric(),
       models: [],
       show_all_models: false,
       judge_model: "",
       reflection_model: "",
       curator_model: "",
       optimizer_scope: "_global",
       expanded_prompt: nil,
       expanded_body: nil,
       # rubrics / pipelines / policies (:unavailable | list)
       rubrics: [],
       pipelines: [],
       bindings: [],
       policies: []
     )}
  end

  @impl true
  def handle_params(params, _uri, socket) do
    tab = if params["tab"] in @tabs, do: params["tab"], else: "sources"
    {:noreply, socket |> assign(tab: tab) |> load_tab()}
  end

  # ── per-tab loading ──────────────────────────────────────────────────

  defp load_tab(%{assigns: %{tab: "sources"}} = socket),
    do: assign(socket, projects: Catalog.list_projects())

  defp load_tab(%{assigns: %{tab: "prompts"}} = socket) do
    socket = if socket.assigns.prompts_loaded, do: socket, else: init_prompts(socket)
    prompts_refresh(socket)
  end

  defp load_tab(%{assigns: %{tab: "rubrics"}} = socket),
    do: assign(socket, rubrics: unwrap(Environment.list_rubrics()))

  defp load_tab(%{assigns: %{tab: "pipelines"}} = socket) do
    assign(socket,
      pipelines: unwrap(Environment.list_pipelines()),
      bindings: unwrap(Environment.list_bindings())
    )
  end

  defp load_tab(%{assigns: %{tab: "policies"}} = socket),
    do: assign(socket, policies: unwrap(Environment.list_policies()))

  # The Environment adapter distinguishes {:ok, rows} from :unavailable
  # (missing DB/table in old data homes); templates case on :unavailable
  # vs [] vs rows, so unwrap the ok-tuple here.
  defp unwrap({:ok, rows}), do: rows
  defp unwrap(:unavailable), do: :unavailable

  defp init_prompts(socket) do
    models = LlmModels.list()
    latest = OptimizerRuns.latest(domain: "_global", target: "judge")

    {judge_default, reflection_default, curator_default} =
      last_used_models(latest, chat_models(models))

    assign(socket,
      prompts_loaded: true,
      models: models,
      judge_model: judge_default,
      reflection_model: reflection_default,
      curator_model: curator_default
    )
  end

  defp last_used_models(
         %{result: %{"judge_model" => j, "reflection_model" => r, "curator_model" => c}},
         _models
       ),
       do: {strip_openai_prefix(j), strip_openai_prefix(r), strip_openai_prefix(c)}

  defp last_used_models(_, models),
    do:
      {Enum.at(models, 0, ""), Enum.at(models, 1, Enum.at(models, 0, "")),
       Enum.at(models, 2, Enum.at(models, 0, ""))}

  defp strip_openai_prefix(nil), do: ""
  defp strip_openai_prefix("openai/" <> rest), do: rest
  defp strip_openai_prefix(other) when is_binary(other), do: other
  defp strip_openai_prefix(_), do: ""

  # ── refresh / optimizer plumbing (prompts tab) ───────────────────────

  @impl true
  def handle_info(:refresh, socket), do: {:noreply, load_tab(socket)}

  def handle_info({:optimizer_line, line}, socket) do
    {:noreply, update(socket, :log_lines, &Enum.take([line | &1], 200))}
  end

  def handle_info({:optimizer_exit, 0}, socket) do
    latest = OptimizerRuns.latest(domain: socket.assigns.optimizer_scope, target: "judge")

    message =
      if latest && latest.result && latest.result["accepted"] do
        "Improved context accepted as a candidate. Review it before promotion."
      else
        "Context evolution completed without a measured gain; production was retained."
      end

    {:noreply,
     socket
     |> assign(optimizer_status: :idle)
     |> put_flash(:info, message)
     |> prompts_refresh()}
  end

  def handle_info({:optimizer_exit, status}, socket) do
    {:noreply,
     socket
     |> assign(optimizer_status: :idle, optimizer_error: "exit #{status}")
     |> put_flash(:error, "Optimizer exited with status #{status}.")
     |> prompts_refresh()}
  end

  # ── events: sources tab (catalog project creation, ADR 0001) ────────

  @impl true
  def handle_event("toggle_project_form", _params, socket) do
    {:noreply,
     assign(socket,
       show_project_form: !socket.assigns.show_project_form,
       project_error: nil
     )}
  end

  def handle_event("create_project", params, socket) do
    name = String.trim(params["name"] || "")
    description = String.trim(params["description"] || "")
    values = %{"name" => name, "description" => description}

    if name == "" do
      {:noreply,
       assign(socket, project_error: "Project name is required.", project_values: values)}
    else
      case catalog_cli(["create-project", "--name", name, "--description", description]) do
        {_out, 0} ->
          {:noreply,
           socket
           |> put_flash(:info, "Project '#{name}' created.")
           |> assign(
             projects: Catalog.list_projects(),
             show_project_form: false,
             project_error: nil,
             project_values: @blank_project
           )}

        {out, status} ->
          {:noreply,
           assign(socket,
             project_error: "create-project failed (exit #{status}):\n#{out}",
             project_values: values
           )}
      end
    end
  end

  # ── events: prompts tab ──────────────────────────────────────────────

  def handle_event("promote", %{"name" => name, "version" => v}, socket) do
    version = String.to_integer(v)

    case LangfusePrompts.promote(name, version) do
      :ok ->
        {:noreply,
         socket
         |> put_flash(:info, "Promoted #{name} v#{version} to production.")
         |> prompts_refresh()}

      {:error, reason} ->
        {:noreply, put_flash(socket, :error, "Promote failed: #{inspect(reason)}")}
    end
  end

  def handle_event("optimize", params, socket) do
    rubric = Map.get(params, "rubric") || Judgement.default_rubric()
    prompt_name = params |> Map.get("prompt_name", "judge-instructions") |> String.trim()
    scope = optimizer_scope(prompt_name)
    judge_model = params |> Map.get("judge_model", "") |> String.trim()
    reflection_model = params |> Map.get("reflection_model", "") |> String.trim()
    curator_model = params |> Map.get("curator_model", "") |> String.trim()

    metric_calls =
      if Map.get(params, "metric_calls") in ~w(24 40 80), do: params["metric_calls"], else: "40"

    run_id =
      OptimizerRuns.start(
        domain: scope,
        target: "judge",
        rubric: rubric,
        prompt_name: prompt_name
      )

    args =
      [
        @optimizer_script,
        "--rubric",
        rubric,
        "--prompt-name",
        prompt_name,
        "--max-metric-calls",
        metric_calls,
        "--run-id",
        Integer.to_string(run_id)
      ]
      |> maybe_append("--model", judge_model)
      |> maybe_append("--reflection-model", reflection_model)
      |> maybe_append("--curator-model", curator_model)
      |> maybe_append("--domain", if(scope == "_global", do: "", else: scope))

    case OptimizerRunner.start(
           "python3",
           args,
           parent: self(),
           rubric_lock: rubric,
           cd: @repo_root_static
         ) do
      {:ok, _pid} ->
        {:noreply,
         socket
         |> assign(
           optimizer_status: :running,
           optimizer_error: nil,
           log_lines: [],
           judge_model: judge_model,
           reflection_model: reflection_model,
           curator_model: curator_model,
           optimizer_scope: scope
         )
         |> put_flash(:info, "Optimizer started. You can leave this page and come back.")
         |> prompts_refresh()}

      {:error, :already_running} ->
        {:noreply, put_flash(socket, :error, "An optimizer is already running for this rubric.")}

      {:error, reason} ->
        {:noreply, put_flash(socket, :error, "Failed to start optimizer: #{inspect(reason)}")}
    end
  end

  def handle_event("toggle_show_all_models", _params, socket) do
    {:noreply, assign(socket, show_all_models: !socket.assigns.show_all_models)}
  end

  def handle_event("toggle_prompt", %{"name" => name}, socket) do
    if socket.assigns.expanded_prompt == name do
      {:noreply, assign(socket, expanded_prompt: nil, expanded_body: nil)}
    else
      body =
        case LangfusePrompts.get(name, "production") do
          text when is_binary(text) ->
            text

          :error ->
            case LangfusePrompts.get(name, "candidate") do
              text when is_binary(text) -> text
              :error -> nil
            end
        end

      {:noreply, assign(socket, expanded_prompt: name, expanded_body: body)}
    end
  end

  defp maybe_append(args, _flag, ""), do: args
  defp maybe_append(args, flag, value), do: args ++ [flag, value]

  defp visible_models(models, true = _show_all), do: models
  defp visible_models(models, false = _show_all), do: chat_models(models)

  defp optimizer_scope("judge-instructions:" <> domain) when domain != "", do: domain
  defp optimizer_scope(_), do: "_global"

  defp optimizer_prompts(prompts) do
    Enum.filter(prompts, &String.starts_with?(&1.name, "judge-instructions"))
  end

  defp preferred_optimizer_scope(current, _prompts) when current != "_global", do: current

  defp preferred_optimizer_scope("_global", prompts) do
    optimizer_prompts = optimizer_prompts(prompts)

    case Enum.find(optimizer_prompts, &(&1.name == "judge-instructions")) do
      nil ->
        case optimizer_prompts do
          [prompt | _] -> optimizer_scope(prompt.name)
          [] -> "_global"
        end

      _global_prompt ->
        "_global"
    end
  end

  defp prompts_refresh(socket) do
    prompts = LangfusePrompts.list()
    scope = preferred_optimizer_scope(socket.assigns.optimizer_scope, prompts)
    latest = OptimizerRuns.latest(domain: scope, target: "judge")

    status =
      case latest do
        %{status: "running"} -> :running
        _ -> :idle
      end

    assign(socket,
      prompts: prompts,
      prompt_backend: LangfusePrompts.backend_info(),
      latest_run: latest,
      optimizer_status: status,
      optimizer_scope: scope
    )
  end

  # ── CLI shell-out (ADR 0001: Python owns all catalog writes) ─────────

  defp catalog_cli(args) do
    [cmd | base] =
      (System.get_env("CATALOG_CLI_CMD") || "python3 bin/catalog.py")
      |> String.split(" ", trim: true)

    System.cmd(cmd, base ++ args, stderr_to_stdout: true, cd: repo_root())
  end

  defp repo_root,
    do: System.get_env("DATA_TOURNAMENTS_REPO") || Path.expand("../../../..", __DIR__)

  # ── render ────────────────────────────────────────────────────────────

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:environment}
      flash={@flash}
      max_width="max-w-6xl"
      title="Environment"
      subtitle="What campaigns run in: evidence sources, prompts, rubrics, pipelines, and policies."
    >
      <div class="flex items-center gap-1 mb-6 border-b app-hairline" id="environment-tabs">
        <.tab_link tab="sources" label="Sources" current={@tab} />
        <.tab_link tab="prompts" label="Prompts" current={@tab} />
        <.tab_link tab="rubrics" label="Rubrics" current={@tab} />
        <.tab_link tab="pipelines" label="Pipelines" current={@tab} />
        <.tab_link tab="policies" label="Policies" current={@tab} />
      </div>

      <%= case @tab do %>
        <% "sources" -> %>
          <.sources_tab
            projects={@projects}
            show_project_form={@show_project_form}
            project_error={@project_error}
            project_values={@project_values}
          />
        <% "prompts" -> %>
          <.prompts_tab
            prompts={@prompts}
            prompt_backend={@prompt_backend}
            default_rubric={@default_rubric}
            latest_run={@latest_run}
            optimizer_status={@optimizer_status}
            log_lines={@log_lines}
            models={@models}
            show_all_models={@show_all_models}
            judge_model={@judge_model}
            reflection_model={@reflection_model}
            curator_model={@curator_model}
            expanded_prompt={@expanded_prompt}
            expanded_body={@expanded_body}
          />
        <% "rubrics" -> %>
          <.rubrics_tab rubrics={@rubrics} />
        <% "pipelines" -> %>
          <.pipelines_tab pipelines={@pipelines} bindings={@bindings} />
        <% "policies" -> %>
          <.policies_tab policies={@policies} />
      <% end %>
    </.workspace_page>
    """
  end

  attr :tab, :string, required: true
  attr :label, :string, required: true
  attr :current, :string, required: true

  defp tab_link(assigns) do
    ~H"""
    <.link
      patch={"/environment?tab=#{@tab}"}
      id={"env-tab-#{@tab}"}
      class={[
        "px-3 py-2 text-sm border-b-2 -mb-px transition",
        @current == @tab && "border-primary font-medium",
        @current != @tab && "border-transparent opacity-60 hover:opacity-100"
      ]}
    >
      {@label}
    </.link>
    """
  end

  # ── sources tab (old /catalog index) ─────────────────────────────────

  attr :projects, :list, required: true
  attr :show_project_form, :boolean, required: true
  attr :project_error, :any, required: true
  attr :project_values, :map, required: true

  defp sources_tab(assigns) do
    ~H"""
    <div id="env-sources">
      <div class="flex items-center justify-between mb-4">
        <p class="text-sm opacity-60">
          Projects under landscape tracking: their components, evidence sources, and snapshots.
        </p>
        <button
          :if={@projects != []}
          type="button"
          id="new-project-btn"
          phx-click="toggle_project_form"
          class="btn btn-primary btn-sm"
        >
          <%= if @show_project_form do %>
            Cancel
          <% else %>
            New project
          <% end %>
        </button>
      </div>

      <%= if @projects == [] do %>
        <div class="app-card p-6" id="catalog-empty">
          <div class="text-sm opacity-70 mb-4">
            No projects in the catalog yet. Register the first one to start
            tracking its components, evidence sources, and snapshots.
          </div>
          <.project_form values={@project_values} error={@project_error} />
        </div>
      <% else %>
        <div class="space-y-3">
          <div :if={@show_project_form} class="app-card p-6">
            <.project_form values={@project_values} error={@project_error} />
          </div>

          <article :for={p <- @projects} class="app-card p-5" id={"catalog-project-#{p.id}"}>
            <div class="flex items-start justify-between gap-4">
              <div class="min-w-0">
                <div class="flex items-center gap-2">
                  <.link
                    navigate={"/catalog/#{p.name}"}
                    class="font-mono text-sm font-semibold hover:text-primary transition"
                  >
                    {p.name}
                  </.link>
                  <span class="text-[11px] font-medium px-2 py-0.5 rounded-full bg-base-200 opacity-70">
                    {p.status}
                  </span>
                </div>
                <div :if={p.description != ""} class="text-sm opacity-80 mt-1">
                  {p.description}
                </div>
              </div>
              <div class="text-xs opacity-55 shrink-0">updated {format_date(p.updated_at)}</div>
            </div>
            <div class="text-xs opacity-55 mt-3 flex gap-4">
              <span>{p.component_count} components</span>
              <span>{p.source_count} sources</span>
              <span>{p.snapshot_count} snapshots</span>
            </div>
          </article>
        </div>
      <% end %>
    </div>
    """
  end

  attr :values, :map, required: true
  attr :error, :any, required: true

  defp project_form(assigns) do
    ~H"""
    <form phx-submit="create_project" id="new-project-form" class="space-y-4">
      <div class="grid sm:grid-cols-2 gap-3">
        <label class="block">
          <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Project name</div>
          <input
            name="name"
            id="new-project-name"
            value={@values["name"]}
            placeholder="e.g. unity-explorer"
            class="input input-bordered input-sm w-full font-mono"
          />
        </label>
        <label class="block">
          <div class="text-xs uppercase tracking-wider opacity-70 mb-1">
            Description <span class="normal-case opacity-50">(optional)</span>
          </div>
          <input
            name="description"
            id="new-project-description"
            value={@values["description"]}
            placeholder="What this project covers"
            class="input input-bordered input-sm w-full"
          />
        </label>
      </div>

      <div :if={@error} id="project-error" class="alert alert-error text-sm whitespace-pre-wrap">
        {@error}
      </div>

      <div class="flex justify-end">
        <button type="submit" class="btn btn-primary btn-sm" id="create-project-btn">
          Create project
        </button>
      </div>
    </form>
    """
  end

  # ── prompts tab (old /prompts) ────────────────────────────────────────

  attr :prompts, :list, required: true
  attr :prompt_backend, :any, required: true
  attr :default_rubric, :any, required: true
  attr :latest_run, :any, required: true
  attr :optimizer_status, :atom, required: true
  attr :log_lines, :list, required: true
  attr :models, :list, required: true
  attr :show_all_models, :boolean, required: true
  attr :judge_model, :string, required: true
  attr :reflection_model, :string, required: true
  attr :curator_model, :string, required: true
  attr :expanded_prompt, :any, required: true
  attr :expanded_body, :any, required: true

  defp prompts_tab(assigns) do
    ~H"""
    <div id="env-prompts">
      <div class="flex items-start justify-between gap-4 flex-wrap mb-5">
        <div :if={@prompt_backend}>
          <div class="text-[10px] uppercase tracking-wider opacity-50">Active backend</div>
          <div class="text-xs font-medium">{@prompt_backend.label}</div>
          <div class="text-[11px] font-mono opacity-50 break-all">{@prompt_backend.location}</div>
        </div>
        <form
          id="context-optimizer-form"
          phx-submit="optimize"
          class="flex items-center gap-2 flex-wrap"
        >
          <input type="hidden" name="rubric" value={@default_rubric} />
          <label class="text-xs opacity-70 flex items-center gap-1">
            Target
            <select name="prompt_name" class="select select-xs select-bordered">
              <%= for prompt <- optimizer_prompts(@prompts) do %>
                <option value={prompt.name}>{prompt.name}</option>
              <% end %>
            </select>
          </label>
          <label class="text-xs opacity-70 flex items-center gap-1">
            Judge
            <select name="judge_model" class="select select-xs select-bordered">
              <option value="">(default)</option>
              <%= for m <- visible_models(@models, @show_all_models) do %>
                <option value={m} selected={m == @judge_model}>{m}</option>
              <% end %>
            </select>
          </label>
          <label class="text-xs opacity-70 flex items-center gap-1">
            Reflection
            <select name="reflection_model" class="select select-xs select-bordered">
              <option value="">(default)</option>
              <%= for m <- visible_models(@models, @show_all_models) do %>
                <option value={m} selected={m == @reflection_model}>{m}</option>
              <% end %>
            </select>
          </label>
          <label class="text-xs opacity-70 flex items-center gap-1">
            Curator
            <select name="curator_model" class="select select-xs select-bordered">
              <option value="">(default)</option>
              <%= for m <- visible_models(@models, @show_all_models) do %>
                <option value={m} selected={m == @curator_model}>{m}</option>
              <% end %>
            </select>
          </label>
          <button
            type="button"
            id="show-all-models-toggle"
            phx-click="toggle_show_all_models"
            class="text-[11px] opacity-60 hover:opacity-100 underline decoration-dotted transition"
            title="Non-chat models (speech, embeddings, moderation…) are hidden by default"
          >
            {if @show_all_models, do: "chat models only", else: "show all models"}
          </button>
          <label class="text-xs opacity-70 flex items-center gap-1">
            Budget
            <select name="metric_calls" class="select select-xs select-bordered">
              <option value="24">24 calls</option>
              <option value="40" selected>40 calls</option>
              <option value="80">80 calls</option>
            </select>
          </label>
          <button
            type="submit"
            class={[
              "btn btn-sm btn-primary",
              @optimizer_status == :running && "btn-disabled"
            ]}
            disabled={@optimizer_status == :running or optimizer_prompts(@prompts) == []}
          >
            {if @optimizer_status == :running, do: "Evolving…", else: "Evolve context"}
          </button>
        </form>
      </div>

      <%= if @latest_run do %>
        <div class={[
          "app-card p-4 mb-5",
          @latest_run.status == "running" && "border-l-4 border-l-primary",
          @latest_run.status == "done" && "border-l-4 border-l-success",
          @latest_run.status == "error" && "border-l-4 border-l-error"
        ]}>
          <div class="flex items-center justify-between gap-3">
            <div>
              <div class="text-sm font-medium">
                Last optimizer run: <span class="font-mono text-xs">{@latest_run.status}</span>
                <%= if @latest_run.result && @latest_run.result["decision"] do %>
                  · {@latest_run.result["decision"]}
                <% end %>
                <%= if @latest_run.result && @latest_run.result["candidate_version"] do %>
                  · candidate v{@latest_run.result["candidate_version"]}
                <% end %>
                <%= if @latest_run.result && @latest_run.result["total_examples"] do %>
                  · {@latest_run.result["total_examples"]} human examples
                <% end %>
              </div>
              <%= if @latest_run.result && @latest_run.result["baseline"] && @latest_run.result["candidate"] do %>
                <div class="text-xs mt-1 font-mono">
                  holdout {percent(@latest_run.result["baseline"]["score"])} → {percent(
                    @latest_run.result["candidate"]["score"]
                  )} · Δ {signed_percent(@latest_run.result["improvement"])} · split {@latest_run.result[
                    "trainset_size"
                  ]}/{@latest_run.result["validation_size"]}/{@latest_run.result["holdout_size"]} · {@latest_run.result[
                    "budget"
                  ]}
                </div>
              <% end %>
              <%= if @latest_run.result && (@latest_run.result["judge_model"] || @latest_run.result["reflection_model"] || @latest_run.result["curator_model"]) do %>
                <div class="text-xs opacity-60 mt-0.5 font-mono">
                  <%= if @latest_run.result["judge_model"] do %>
                    judge: {@latest_run.result["judge_model"]}
                  <% end %>
                  <%= if @latest_run.result["reflection_model"] do %>
                    · reflection: {@latest_run.result["reflection_model"]}
                  <% end %>
                  <%= if @latest_run.result["curator_model"] do %>
                    · curator: {@latest_run.result["curator_model"]}
                  <% end %>
                </div>
              <% end %>
              <div class="text-xs opacity-60 mt-0.5 font-mono">
                started {@latest_run.started_at}
                <%= if @latest_run.finished_at do %>
                  · finished {@latest_run.finished_at}
                <% end %>
              </div>
            </div>
          </div>
          <%= if @latest_run.log && @latest_run.log != "" do %>
            <details class="mt-3">
              <summary class="text-xs opacity-70 cursor-pointer hover:opacity-100">
                Log ({String.length(@latest_run.log)} chars)
              </summary>
              <pre class="mt-2 text-xs font-mono bg-base-200 p-3 rounded max-h-64 overflow-y-auto whitespace-pre-wrap">{@latest_run.log}</pre>
            </details>
          <% end %>
        </div>
      <% end %>

      <%= if @prompts == [] do %>
        <div class="app-card p-8 text-center">
          <div class="text-sm font-medium">
            No prompts in {(@prompt_backend && @prompt_backend.label) || "the prompt store"} yet
          </div>
          <div :if={@prompt_backend} class="text-xs opacity-60 mt-1 font-mono break-all">
            {@prompt_backend.location}
          </div>
          <p class="text-sm opacity-65 mt-3">
            Create a domain or initialize the judgement fabric to seed production prompts.
          </p>
        </div>
      <% else %>
        <div class="space-y-4">
          <%= for p <- @prompts do %>
            <div class="app-card p-5">
              <div class="flex items-start justify-between gap-4">
                <button
                  type="button"
                  phx-click="toggle_prompt"
                  phx-value-name={p.name}
                  class="min-w-0 flex-1 text-left cursor-pointer group"
                  title="Click to view the prompt text"
                >
                  <div class="font-mono text-sm font-semibold truncate group-hover:underline decoration-dotted">
                    {p.name}
                    <span class="ml-1 text-xs opacity-40 no-underline">
                      {if @expanded_prompt == p.name, do: "▾", else: "▸"}
                    </span>
                  </div>
                  <div class="text-xs opacity-60 mt-1">
                    versions: {Enum.join(Enum.map(p.all_versions, &"v#{&1}"), ", ")}
                    <%= if p.production_version do %>
                      · <span class="font-mono">production: v{p.production_version}</span>
                    <% end %>
                    <%= if p.candidate_version do %>
                      · <span class="font-mono">candidate: v{p.candidate_version}</span>
                    <% end %>
                  </div>
                </button>
                <div class="flex gap-2 shrink-0">
                  <%= if p.candidate_version && p.candidate_version != p.production_version do %>
                    <button
                      phx-click="promote"
                      phx-value-name={p.name}
                      phx-value-version={p.candidate_version}
                      class="btn btn-primary btn-sm"
                    >
                      Promote v{p.candidate_version}
                    </button>
                  <% end %>
                </div>
              </div>
              <%= if @expanded_prompt == p.name do %>
                <div class="mt-3 border-t app-hairline pt-3" id={"prompt-body-#{p.name}"}>
                  <%= if @expanded_body do %>
                    <pre class="text-xs font-mono bg-base-200 p-3 rounded max-h-96 overflow-y-auto whitespace-pre-wrap">{@expanded_body}</pre>
                  <% else %>
                    <p class="text-xs opacity-60">
                      Couldn't load the prompt text (no production or candidate label reachable).
                    </p>
                  <% end %>
                </div>
              <% end %>
            </div>
          <% end %>
        </div>
      <% end %>

      <%= if @log_lines != [] or @optimizer_status == :running do %>
        <div class="app-card mt-6 p-4">
          <div class="flex items-center justify-between mb-2">
            <div class="text-xs uppercase tracking-wider opacity-60">Optimizer log</div>
            <div class="text-xs font-mono opacity-50">
              <%= case @optimizer_status do %>
                <% :running -> %>
                  running…
                <% :idle -> %>
                  idle
              <% end %>
            </div>
          </div>
          <pre class="font-mono text-xs leading-5 max-h-64 overflow-y-auto"><%= for l <- Enum.reverse(@log_lines) do %>{l}
          <% end %></pre>
        </div>
      <% end %>
    </div>
    """
  end

  # ── rubrics tab ───────────────────────────────────────────────────────

  attr :rubrics, :any, required: true

  defp rubrics_tab(assigns) do
    ~H"""
    <div id="env-rubrics">
      <%= case @rubrics do %>
        <% :unavailable -> %>
          <.not_initialized id="rubrics" what="Rubrics (eval_template)" />
        <% [] -> %>
          <div class="app-card p-8 text-center text-sm opacity-70" id="env-rubrics-empty">
            No rubrics registered yet. Initializing the judgement fabric seeds the default rubric.
          </div>
        <% rubrics -> %>
          <div class="space-y-2">
            <article
              :for={r <- rubrics}
              class="app-card px-5 py-3"
              id={"rubric-#{r.name}-v#{r.version}"}
            >
              <div class="flex items-center gap-2 flex-wrap">
                <span class="font-mono text-sm font-semibold">{r.name}</span>
                <span class="text-xs font-mono opacity-50">v{r.version}</span>
                <span class={[
                  "text-[10px] font-semibold px-1.5 py-0.5 rounded",
                  r.judgement_kind == "single" && "bg-info/15 text-info",
                  r.judgement_kind != "single" && "bg-base-200 opacity-80"
                ]}>
                  {String.upcase(r.judgement_kind)}
                </span>
                <span
                  :for={subject <- r.subjects}
                  class="text-[10px] px-1.5 py-0.5 rounded bg-base-200 opacity-70"
                >
                  {subject}
                </span>
                <span
                  :if={r.wheel?}
                  class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-success/15 text-success"
                  title="this rubric defines a verdict wheel"
                >
                  wheel
                </span>
                <span class="text-xs opacity-40 ml-auto">{format_date(r.created_at)}</span>
              </div>
              <div :if={r.verdict_enum != []} class="text-xs font-mono opacity-55 mt-1.5">
                verdicts: {Enum.join(r.verdict_enum, " / ")}
              </div>
            </article>
          </div>
      <% end %>
    </div>
    """
  end

  # ── pipelines tab ─────────────────────────────────────────────────────

  attr :pipelines, :any, required: true
  attr :bindings, :any, required: true

  defp pipelines_tab(assigns) do
    ~H"""
    <div id="env-pipelines" class="space-y-6">
      <section>
        <h2 class="text-xs uppercase tracking-widest opacity-60 mb-3">Registry</h2>
        <%= case @pipelines do %>
          <% :unavailable -> %>
            <.not_initialized id="pipelines" what="Pipelines" />
          <% [] -> %>
            <div class="app-card p-8 text-center text-sm opacity-70" id="env-pipelines-empty">
              No pipelines registered yet (bin/pipelines.py register / seed-defaults).
            </div>
          <% pipelines -> %>
            <div class="space-y-2">
              <article
                :for={p <- pipelines}
                class="app-card px-5 py-3"
                id={"pipeline-#{p.name}-v#{p.version}"}
              >
                <div class="flex items-center gap-2 flex-wrap">
                  <span class="font-mono text-sm font-semibold">{p.name}</span>
                  <span class="text-xs font-mono opacity-50">v{p.version}</span>
                  <span class="font-mono text-[10px] opacity-40" title={p.digest}>
                    {String.slice(p.digest, 0, 12)}
                  </span>
                  <span class="text-xs opacity-40 ml-auto">{format_date(p.created_at)}</span>
                </div>
                <div class="flex items-center gap-1.5 mt-2 flex-wrap">
                  <%= for {stage, i} <- Enum.with_index(p.stages) do %>
                    <span :if={i > 0} class="text-xs opacity-30" aria-hidden="true">→</span>
                    <span class={[
                      "text-[10px] font-mono px-1.5 py-0.5 rounded",
                      stage["action"] && "bg-warning/10 text-warning",
                      !stage["action"] && "bg-base-200 opacity-80"
                    ]}>
                      {stage["key"]} · {stage_summary(stage)}
                    </span>
                  <% end %>
                </div>
              </article>
            </div>
        <% end %>
      </section>

      <section>
        <h2 class="text-xs uppercase tracking-widest opacity-60 mb-3">Domain bindings</h2>
        <%= case @bindings do %>
          <% :unavailable -> %>
            <.not_initialized id="bindings" what="Domain bindings" />
          <% [] -> %>
            <p class="text-sm opacity-60" id="env-bindings-empty">
              No domain is bound to a pipeline yet (bindings are permanent; bin/pipelines.py bind).
            </p>
          <% bindings -> %>
            <div class="overflow-x-auto app-card">
              <table class="table table-sm w-full" id="env-bindings">
                <thead>
                  <tr class="text-xs uppercase tracking-wide opacity-60">
                    <th>Domain</th>
                    <th>Pipeline</th>
                    <th>Bound</th>
                  </tr>
                </thead>
                <tbody>
                  <tr :for={b <- bindings} id={"binding-#{b.domain}"}>
                    <td class="font-mono text-sm">{b.domain}</td>
                    <td class="font-mono text-sm">{b.pipeline} v{b.version}</td>
                    <td class="text-xs opacity-55">{format_date(b.created_at)}</td>
                  </tr>
                </tbody>
              </table>
            </div>
        <% end %>
      </section>
    </div>
    """
  end

  defp stage_summary(%{"action" => action}), do: action

  defp stage_summary(stage),
    do: "#{stage["judgement"] || "?"} #{stage["subject"] || "?"}"

  # ── policies tab ──────────────────────────────────────────────────────

  attr :policies, :any, required: true

  defp policies_tab(assigns) do
    ~H"""
    <div id="env-policies">
      <p class="text-xs opacity-55 mb-3">
        Approver and scope names only — rule bodies and secret values are never shown here.
      </p>
      <%= case @policies do %>
        <% :unavailable -> %>
          <.not_initialized id="policies" what="Policies" />
        <% [] -> %>
          <div class="app-card p-8 text-center text-sm opacity-70" id="env-policies-empty">
            No policies exist. Approvals fail closed until one is created
            (bin/catalog.py create-policy --kind approval).
          </div>
        <% policies -> %>
          <div class="overflow-x-auto app-card">
            <table class="table table-sm w-full" id="env-policies-table">
              <thead>
                <tr class="text-xs uppercase tracking-wide opacity-60">
                  <th>Name</th>
                  <th>Kind</th>
                  <th>Scope</th>
                  <th>Approvers</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                <tr :for={p <- policies} id={"policy-#{p.name}"}>
                  <td class="font-mono text-sm font-semibold">{p.name}</td>
                  <td>
                    <span class="text-[11px] font-medium px-2 py-0.5 rounded-full bg-base-200">
                      {p.kind}
                    </span>
                  </td>
                  <td class="font-mono text-xs">{p.scope || "—"}</td>
                  <td class="text-xs">
                    <%= if p.approvers == [] do %>
                      <span class="opacity-40">—</span>
                    <% else %>
                      {Enum.join(p.approvers, ", ")}
                    <% end %>
                  </td>
                  <td class="text-xs opacity-60">{p.status}</td>
                </tr>
              </tbody>
            </table>
          </div>
      <% end %>
    </div>
    """
  end

  attr :what, :string, required: true
  attr :id, :string, required: true

  defp not_initialized(assigns) do
    ~H"""
    <div class="app-card p-6 text-sm opacity-70" id={"env-not-initialized-#{@id}"}>
      {@what} are not initialized in this data home — the table does not exist yet.
      Run the fabric init (bin/judgement.py init or bin/catalog.py init) to create it.
    </div>
    """
  end

  # ── shared helpers ────────────────────────────────────────────────────

  defp percent(value) when is_number(value), do: "#{Float.round(value * 100.0, 1)}%"
  defp percent(_), do: "—"

  defp signed_percent(value) when is_number(value) do
    sign = if value > 0, do: "+", else: ""
    "#{sign}#{Float.round(value * 100.0, 1)}pp"
  end

  defp signed_percent(_), do: "—"

  defp format_date(nil), do: "—"
  defp format_date(value) when is_binary(value), do: String.slice(value, 0, 10)
end
