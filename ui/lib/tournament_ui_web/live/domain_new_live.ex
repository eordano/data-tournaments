defmodule TournamentUiWeb.DomainNewLive do
  @moduledoc """
  /domains/new — two-stage form:
    1. Describe + pick corpus source → click "Draft prompts"
    2. Edit AI-drafted name + generator + judge prompts → "Save domain"

  Both --draft and --save shell-outs run from the repo root (see repo_root/0).
  """
  use TournamentUiWeb, :live_view

  @code_globs "**/*.py, **/*.cs, **/*.ex, **/*.exs, **/*.js, **/*.jsx, **/*.ts, **/*.tsx, **/*.go, **/*.rs"

  # Read at runtime (not compile time) so tests and deploys can point these
  # at stubs/other checkouts without recompiling.
  defp builder_script, do: System.get_env("DOMAIN_BUILDER_SCRIPT") || "bin/domain_builder_cli.py"

  defp repo_root,
    do: System.get_env("DATA_TOURNAMENTS_REPO") || Path.expand("../../../..", __DIR__)

  @starters %{
    "correctness" => %{
      title: "Correctness & reliability",
      description:
        "Find concrete correctness and reliability risks in this code. Produce only findings backed by a specific behavior or location; prioritize user impact and reproducibility."
    },
    "security" => %{
      title: "Security & privacy",
      description:
        "Find concrete security and privacy risks in this code. Produce only findings with a defensible trust boundary or data-flow concern; prioritize severity, exploitability, and confidence."
    },
    "maintainability" => %{
      title: "Architecture & maintainability",
      description:
        "Find architecture and maintainability problems in this code. Produce specific findings about coupling, duplication, or change friction; prioritize blast radius, leverage, and actionability."
    },
    "testing" => %{
      title: "Tests & observability",
      description:
        "Find missing tests and observability gaps in this code. Produce specific, risk-linked findings; prioritize regression risk, diagnostic value, and the clarity of the next test or signal to add."
    },
    "conventions" => %{
      title: "Conventions & consistency",
      description:
        "Extract code conventions worth standardizing across this corpus. Produce concrete, recurring, enforceable patterns rather than one-off style preferences."
    },
    "custom" => %{title: "Custom category", description: ""}
  }

  @impl true
  def mount(params, _session, socket) do
    starter_id = Map.get(params, "starter", "custom")
    starter = Map.get(@starters, starter_id, @starters["custom"])

    # Blank-state bootstrap (journey finding: fresh data dir + save ->
    # raw 'no such table: domain'). Same idempotent init /judge uses;
    # failure degrades to a visible warning, never a raw SQL error later.
    init_warning =
      case TournamentUi.Judgement.ensure_initialized() do
        :ok -> nil
        {:error, reason} -> "Workspace database not initialized: #{reason}"
      end

    {:ok,
     socket
     |> assign(
       stage: 1,
       starter_id: starter_id,
       starter_title: starter.title,
       requested_name: "",
       description: starter.description,
       corpus_kind: if(starter_id == "custom", do: "inline", else: "filesystem"),
       corpus_path: "",
       corpus_query: "",
       corpus_glob: if(starter_id == "custom", do: "*.md", else: @code_globs),
       corpus_inline: "",
       draft: nil,
       drafting: false,
       draft_error: nil,
       save_error: nil,
       init_warning: init_warning
     )}
  end

  @impl true
  def handle_event("change_stage1", params, socket) do
    {:noreply,
     assign(socket,
       requested_name: params["requested_name"] || socket.assigns.requested_name,
       description: params["description"] || socket.assigns.description,
       corpus_kind: params["corpus_kind"] || socket.assigns.corpus_kind,
       corpus_path: params["corpus_path"] || socket.assigns.corpus_path,
       corpus_query: params["corpus_query"] || socket.assigns.corpus_query,
       corpus_glob: params["corpus_glob"] || socket.assigns.corpus_glob,
       corpus_inline: params["corpus_inline"] || socket.assigns.corpus_inline
     )}
  end

  def handle_event("draft", _params, socket) do
    if socket.assigns.drafting do
      {:noreply, socket}
    else
      with :ok <- validate_domain_name(socket.assigns.requested_name),
           :ok <- validate_corpus(socket.assigns) do
        spec_json = build_corpus_spec(socket.assigns)
        description = socket.assigns.description
        script = builder_script()
        repo = repo_root()

        args = [script, "--description", description, "--corpus-spec", Jason.encode!(spec_json)]

        {:noreply,
         socket
         |> assign(drafting: true, draft_error: nil)
         |> start_async(:draft, fn ->
           System.cmd("python3", args, stderr_to_stdout: true, cd: repo)
         end)}
      else
        {:error, message} ->
          {:noreply, assign(socket, draft_error: message)}
      end
    end
  end

  def handle_event("change_draft", %{"draft" => draft}, socket) do
    {:noreply, assign(socket, draft: Map.merge(socket.assigns.draft || %{}, draft))}
  end

  def handle_event("back_to_stage1", _params, socket) do
    {:noreply, assign(socket, stage: 1, draft: nil, save_error: nil)}
  end

  def handle_event("save", _params, socket) do
    spec_json = build_corpus_spec(socket.assigns)
    draft = socket.assigns.draft || %{}

    save_args = [
      builder_script(),
      "--save",
      "--name",
      draft["domain_name"] || "",
      "--description",
      socket.assigns.description,
      "--generator-prompt",
      draft["generator_prompt"] || "",
      "--judge-prompt",
      draft["judge_prompt"] || "",
      "--corpus-spec",
      Jason.encode!(spec_json)
    ]

    case System.cmd("python3", save_args, stderr_to_stdout: true, cd: repo_root()) do
      {_, 0} ->
        {:noreply,
         socket
         |> put_flash(
           :info,
           "Domain '#{draft["domain_name"]}' created. Generate its first candidate batch next."
         )
         |> push_navigate(to: "/domains")}

      {output, status} ->
        {:noreply, assign(socket, save_error: "Save failed (exit #{status}):\n#{output}")}
    end
  end

  @impl true
  def handle_async(:draft, {:ok, {output, 0}}, socket) do
    case extract_draft_json(output) do
      {:ok, draft} ->
        # A user-chosen name is authoritative: the AI's suggestion only
        # fills the gap when the Stage-1 field was left blank.
        draft =
          case String.trim(socket.assigns.requested_name) do
            "" -> draft
            name -> Map.put(draft, "domain_name", name)
          end

        {:noreply, assign(socket, drafting: false, stage: 2, draft: draft, draft_error: nil)}

      {:error, reason} ->
        {:noreply,
         assign(socket,
           drafting: false,
           draft_error: "Couldn't parse draft: #{reason}\n#{output}"
         )}
    end
  end

  def handle_async(:draft, {:ok, {output, status}}, socket) do
    {:noreply,
     assign(socket, drafting: false, draft_error: "Drafting failed (exit #{status}):\n#{output}")}
  end

  def handle_async(:draft, {:exit, reason}, socket) do
    {:noreply,
     assign(socket, drafting: false, draft_error: "Drafting crashed: #{inspect(reason)}")}
  end

  # Fail fast on inputs that can never draft, instead of burning an LM call.
  # Domain names become URL segments (/domains/<name>/edit) and prompt keys
  # (card-generator:<name>), so only a conservative slug is accepted.
  defp validate_domain_name(""), do: :ok

  defp validate_domain_name(name) when is_binary(name) do
    trimmed = String.trim(name)

    cond do
      trimmed == "" ->
        :ok

      String.length(trimmed) > 64 ->
        {:error, "Domain name is too long (max 64 characters)."}

      not Regex.match?(~r/^[a-z0-9][a-z0-9-]*$/, trimmed) ->
        {:error,
         "Domain name must be a slug: lowercase letters, digits, and hyphens, " <>
           "starting with a letter or digit (e.g. unity-explorer-correctness)."}

      true ->
        :ok
    end
  end

  defp validate_corpus(%{corpus_kind: "filesystem", corpus_path: path}) do
    cond do
      String.starts_with?(path, ["http://", "https://", "git@"]) ->
        {:error,
         "Root path looks like a URL. The filesystem source reads a local directory — " <>
           "clone the repository first and point at the clone (e.g. /Users/you/src/repo)."}

      path == "" or not File.dir?(path) ->
        {:error, "Root path #{inspect(path)} is not a directory on this machine."}

      true ->
        :ok
    end
  end

  defp validate_corpus(%{corpus_kind: "sqlite", corpus_path: path}) do
    if path != "" and File.exists?(path),
      do: :ok,
      else: {:error, "SQLite DB #{inspect(path)} does not exist on this machine."}
  end

  defp validate_corpus(%{corpus_kind: "inline", corpus_inline: inline}) do
    if String.trim(inline || "") == "",
      do: {:error, "Paste at least one item (one per line) before drafting."},
      else: :ok
  end

  defp validate_corpus(_), do: :ok

  # New domains generate rich WorkOrder documents by default (user: legacy
  # cards are "too small" to judge). The pipeline falls back to compact
  # cards only for OLD stored domains whose corpus_source predates the
  # artifact key (bin/generate_cards.py: corpus_source.get("artifact",
  # "card")).
  defp build_corpus_spec(a) do
    base =
      case a.corpus_kind do
        "inline" ->
          items =
            (a.corpus_inline || "")
            |> String.split("\n", trim: true)
            |> Enum.map(&%{"text" => &1})

          %{"kind" => "inline", "items" => items}

        "filesystem" ->
          %{"kind" => "filesystem", "root" => a.corpus_path, "glob" => a.corpus_glob}

        "sqlite" ->
          %{"kind" => "sqlite", "path" => a.corpus_path, "query" => a.corpus_query}
      end

    Map.put(base, "artifact", "work-order")
  end

  defp extract_draft_json(stdout) do
    case Regex.run(~r/^DRAFT_JSON:\s*(\{.*\})\s*$/m, stdout) do
      [_, json] ->
        case Jason.decode(json) do
          {:ok, draft} -> {:ok, draft}
          {:error, e} -> {:error, inspect(e)}
        end

      nil ->
        {:error, "no DRAFT_JSON marker in output"}
    end
  end

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:domains}
      flash={@flash}
      max_width="max-w-3xl"
      title="New domain"
      subtitle={stage_subtitle(@stage)}
    >
      <:nav_actions>
        <.link navigate="/start" class="text-sm opacity-60 hover:opacity-100">
          ← choose another category
        </.link>
      </:nav_actions>

      <%= if @init_warning do %>
        <div class="alert alert-warning text-sm mb-4" id="init-warning">
          {@init_warning} — drafting still works; saving needs the workspace
          database. Retry by reloading this page.
        </div>
      <% end %>

      <%= if @stage == 1 do %>
        <.stage1
          description={@description}
          starter_title={@starter_title}
          requested_name={@requested_name}
          corpus_kind={@corpus_kind}
          corpus_path={@corpus_path}
          corpus_query={@corpus_query}
          corpus_glob={@corpus_glob}
          corpus_inline={@corpus_inline}
          draft_error={@draft_error}
          drafting={@drafting}
        />
      <% else %>
        <.stage2
          description={@description}
          corpus_kind={@corpus_kind}
          draft={@draft}
          save_error={@save_error}
        />
      <% end %>
    </.workspace_page>
    """
  end

  defp stage_subtitle(1),
    do: "Step 1 of 2 — describe what you want to extract and where the corpus lives."

  defp stage_subtitle(2),
    do: "Step 2 of 2 — review the AI-drafted prompts, edit as needed, then save."

  defp stage_subtitle(_), do: nil

  attr :description, :string, required: true
  attr :starter_title, :string, required: true
  attr :requested_name, :string, required: true
  attr :corpus_kind, :string, required: true
  attr :corpus_path, :string, required: true
  attr :corpus_query, :string, required: true
  attr :corpus_glob, :string, required: true
  attr :corpus_inline, :string, required: true
  attr :draft_error, :any, required: true
  attr :drafting, :boolean, required: true

  defp stage1(assigns) do
    ~H"""
    <form phx-change="change_stage1" phx-submit="draft" id="stage1" class="space-y-6">
      <div id="selected-evaluation-lens" class="app-card p-5 border-l-4 border-l-primary">
        <div class="text-xs uppercase tracking-wider opacity-55">Evaluation lens</div>
        <div class="font-semibold mt-1">{@starter_title}</div>
        <p class="text-sm opacity-65 mt-1">
          Keep one category per domain so every pair can be judged against the same question.
        </p>
      </div>

      <div class="app-card p-5">
        <label class="block">
          <div class="text-sm font-medium mb-1">
            Domain name <span class="opacity-50 font-normal">(optional)</span>
          </div>
          <input
            id="domain-name"
            name="requested_name"
            value={@requested_name}
            placeholder="e.g. unity-explorer-correctness — leave blank to let the AI suggest one"
            class="input input-bordered w-full font-mono text-sm"
          />
          <div class="text-xs opacity-60 mt-1">
            Lowercase letters, digits, and hyphens. Used in URLs and prompt names; you can still edit it before saving.
          </div>
        </label>
      </div>

      <div class="app-card p-5">
        <label class="block">
          <div class="text-sm font-medium mb-1">Define what should become a candidate</div>
          <input
            name="description"
            value={@description}
            placeholder="e.g. Extract durable memories from chat archives"
            class="input input-bordered w-full"
          />
          <div class="text-xs opacity-60 mt-1">
            Be explicit about evidence and exclusions. The AI will draft generation and judging instructions from this goal.
          </div>
        </label>
      </div>

      <div class="app-card p-5">
        <div class="text-sm font-medium mb-1">Choose the source corpus</div>
        <p class="text-xs opacity-60 mb-3">
          Each source item can generate zero or more focused candidates.
        </p>
        <div class="flex gap-2 mb-4">
          <%= for kind <- ~w(inline filesystem sqlite) do %>
            <label class={[
              "btn btn-sm",
              @corpus_kind == kind && "btn-primary",
              @corpus_kind != kind && "btn-ghost border app-hairline"
            ]}>
              <input
                type="radio"
                name="corpus_kind"
                value={kind}
                checked={@corpus_kind == kind}
                class="hidden"
              />
              {kind}
            </label>
          <% end %>
        </div>

        <%= case @corpus_kind do %>
          <% "inline" -> %>
            <label class="block">
              <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Items (one per line)</div>
              <textarea
                name="corpus_inline"
                class="textarea textarea-bordered w-full h-32 font-mono text-xs"
                placeholder="paste sample items, one per line"
              ><%= @corpus_inline %></textarea>
            </label>
          <% "filesystem" -> %>
            <div class="grid grid-cols-2 gap-3">
              <label class="block">
                <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Root path</div>
                <input
                  name="corpus_path"
                  value={@corpus_path}
                  placeholder="$HOME/projects/foo"
                  class="input input-bordered input-sm w-full font-mono"
                />
              </label>
              <label class="block">
                <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Globs</div>
                <input
                  name="corpus_glob"
                  value={@corpus_glob}
                  placeholder="**/*.ts, **/*.tsx"
                  class="input input-bordered input-sm w-full font-mono"
                />
                <div class="text-[11px] opacity-55 mt-1">
                  Comma-separated; dependencies and build output are skipped.
                </div>
              </label>
            </div>
          <% "sqlite" -> %>
            <div class="space-y-3">
              <label class="block">
                <div class="text-xs uppercase tracking-wider opacity-70 mb-1">DB path</div>
                <input
                  name="corpus_path"
                  value={@corpus_path}
                  placeholder="$HOME/.hermes/sessions.db"
                  class="input input-bordered input-sm w-full font-mono"
                />
              </label>
              <label class="block">
                <div class="text-xs uppercase tracking-wider opacity-70 mb-1">
                  Query (must return id, text, source_ref)
                </div>
                <textarea
                  name="corpus_query"
                  class="textarea textarea-bordered w-full h-24 font-mono text-xs"
                  placeholder="SELECT id, content AS text, role AS source_ref FROM messages LIMIT 500"
                ><%= @corpus_query %></textarea>
              </label>
            </div>
        <% end %>
      </div>

      <%= if @draft_error do %>
        <div class="alert alert-error text-sm whitespace-pre-wrap">{@draft_error}</div>
      <% end %>

      <div class="flex justify-end items-center gap-3">
        <%= if @drafting do %>
          <span class="text-sm opacity-70" id="drafting-status">
            Drafting with the AI… this usually takes 10–30 seconds.
          </span>
        <% end %>
        <button type="submit" class="btn btn-primary" disabled={@drafting} id="draft-prompts-btn">
          <%= if @drafting do %>
            <span class="loading loading-spinner loading-xs"></span> Drafting…
          <% else %>
            Draft prompts →
          <% end %>
        </button>
      </div>
    </form>
    """
  end

  attr :description, :string, required: true
  attr :corpus_kind, :string, required: true
  attr :draft, :any, required: true
  attr :save_error, :any, required: true

  defp stage2(assigns) do
    ~H"""
    <form phx-change="change_draft" phx-submit="save" id="stage2" class="space-y-5">
      <div class="app-card p-5">
        <div class="text-xs uppercase tracking-wider opacity-60 mb-2">Description (locked)</div>
        <div class="text-sm">{@description}</div>
        <div class="text-xs opacity-60 mt-1">
          Corpus: <span class="font-mono">{@corpus_kind}</span>
        </div>
      </div>

      <label class="app-card p-5 block">
        <div class="text-sm font-medium mb-1">Domain name</div>
        <input
          name="draft[domain_name]"
          value={Map.get(@draft || %{}, "domain_name", "")}
          class="input input-bordered w-full font-mono"
        />
        <div class="text-xs opacity-60 mt-1">
          Lowercase, hyphenated. Used as Langfuse prompt suffix.
        </div>
      </label>

      <label class="app-card p-5 block">
        <div class="text-sm font-medium mb-1">Generation instructions</div>
        <div class="text-xs opacity-60 mb-2">
          Turns one corpus item into zero or more comparable candidates in this category.
        </div>
        <textarea
          name="draft[generator_prompt]"
          class="textarea textarea-bordered prompt-editor w-full h-48 font-mono text-xs"
        ><%= Map.get(@draft || %{}, "generator_prompt", "") %></textarea>
      </label>

      <label class="app-card p-5 block">
        <div class="text-sm font-medium mb-1">Judging brief</div>
        <div class="text-xs opacity-60 mb-2">
          Keeps every pairwise decision anchored to the same category and priority criteria.
        </div>
        <textarea
          name="draft[judge_prompt]"
          class="textarea textarea-bordered prompt-editor w-full h-48 font-mono text-xs"
        ><%= Map.get(@draft || %{}, "judge_prompt", "") %></textarea>
      </label>

      <%= if @save_error do %>
        <div class="alert alert-error text-sm whitespace-pre-wrap">{@save_error}</div>
      <% end %>

      <div class="flex justify-between">
        <button type="button" phx-click="back_to_stage1" class="btn btn-ghost">← back</button>
        <button type="submit" class="btn btn-primary">Save domain →</button>
      </div>
    </form>
    """
  end
end
