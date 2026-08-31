defmodule TournamentUiWeb.DesignerLive do
  @moduledoc """
  /designer — the visual sweep composer (ComfyUI-style node canvas).

  The pipeline is drawn as typed nodes (corpus sources, intake gate, lens
  panel, human panel, rounds, validation, publish) with derived wires;
  clicking a node opens its editor in the sidebar, dragging rearranges the
  canvas, and the whole graph compiles LIVE into the SweepSpec JSON that
  campaigns freeze at creation. "Validate" runs the real
  `bin/campaigns.py validate-spec` (pydantic is the authority, never a
  re-implementation), and "Create campaign" shells the same
  create-campaign path the /campaigns form uses.

  Graph semantics live in TournamentUi.SweepGraph; this module is canvas,
  editing, and the CLI seam.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.Catalog
  alias TournamentUi.SweepGraph

  @kinds ~w(bugsweep perfsweep featuresweep slopsweep)
  @templates %{
    "bugsweep" => "bugsweep-example.json",
    "perfsweep" => "perfsweep-example.json",
    "featuresweep" => "featuresweep-foundry.json",
    "slopsweep" => "slopsweep-example.json"
  }

  @node_style %{
    corpus: {"Corpus", "#3d7fa6"},
    intake: {"Intake gate", "#a67c3d"},
    lens: {"Lens", "#7a5cc2"},
    human: {"Human panel", "#b05680"},
    rounds: {"Rounds", "#c26a2e"},
    validation: {"Validation", "#3f8f63"},
    publish: {"Publish gate", "#5a6472"},
    runner: {"Runner", "#2e8f8f"}
  }

  defp cli_cmd, do: System.get_env("CAMPAIGNS_CLI_CMD") || "python3 bin/campaigns.py"

  defp repo_root,
    do: System.get_env("DATA_TOURNAMENTS_REPO") || Path.expand("../../../..", __DIR__)

  @impl true
  def mount(_params, _session, socket) do
    {:ok,
     socket
     |> assign(
       kind: "featuresweep",
       selected: nil,
       check: nil,
       campaign_name: "",
       project: "",
       projects: Catalog.list_projects(),
       create_result: nil,
       export_result: nil
     )
     |> load_template("featuresweep")}
  end

  defp load_template(socket, kind) do
    path = Path.join([repo_root(), "configs", "sweeps", @templates[kind]])

    nodes =
      case File.read(path) do
        {:ok, raw} -> raw |> Jason.decode!() |> SweepGraph.from_spec()
        _ -> SweepGraph.from_spec(%{"kind" => kind})
      end

    socket |> assign(kind: kind, nodes: nodes, selected: nil, check: nil) |> recompile()
  end

  defp recompile(socket) do
    spec_json =
      try do
        socket.assigns.nodes
        |> SweepGraph.to_spec(socket.assigns.kind)
        |> Jason.encode!(pretty: true)
      rescue
        e in ArgumentError -> {:error, Exception.message(e)}
      end

    assign(socket, spec_json: spec_json)
  end

  @impl true
  def handle_event("set_kind", %{"kind" => kind}, socket) when kind in @kinds do
    {:noreply, load_template(socket, kind)}
  end

  def handle_event("select_node", %{"id" => id}, socket) do
    {:noreply, assign(socket, selected: id)}
  end

  def handle_event("deselect", _params, socket) do
    {:noreply, assign(socket, selected: nil)}
  end

  def handle_event("node_moved", %{"id" => id, "x" => x, "y" => y}, socket) do
    {:noreply,
     assign(socket, nodes: SweepGraph.move_node(socket.assigns.nodes, id, round(x), round(y)))}
  end

  def handle_event("add_lens", _params, socket) do
    {:noreply, socket |> assign(nodes: SweepGraph.add_lens(socket.assigns.nodes)) |> recompile()}
  end

  def handle_event("add_corpus", _params, socket) do
    {:noreply,
     socket |> assign(nodes: SweepGraph.add_corpus(socket.assigns.nodes)) |> recompile()}
  end

  def handle_event("toggle_human", _params, socket) do
    {:noreply,
     socket |> assign(nodes: SweepGraph.toggle_human(socket.assigns.nodes)) |> recompile()}
  end

  def handle_event("remove_node", %{"id" => id}, socket) do
    {:noreply,
     socket
     |> assign(nodes: SweepGraph.remove_node(socket.assigns.nodes, id), selected: nil)
     |> recompile()}
  end

  def handle_event("update_node", %{"node" => %{"id" => id} = params}, socket) do
    data = Map.drop(params, ["id", "_target"])

    data =
      if Enum.any?(socket.assigns.nodes, &(&1.id == id and &1.type in [:human, :intake])) and
           not Map.has_key?(params, "required") and not Map.has_key?(params, "rationale_required") do
        checkbox_off(data, id, socket.assigns.nodes)
      else
        data
      end

    {:noreply,
     socket
     |> assign(nodes: SweepGraph.update_node(socket.assigns.nodes, id, data))
     |> recompile()}
  end

  def handle_event("create_meta", %{"create" => params}, socket) do
    {:noreply,
     assign(socket,
       campaign_name: String.trim(params["name"] || ""),
       project: params["project"] || ""
     )}
  end

  def handle_event("export_pack", _params, socket) do
    name =
      case socket.assigns.campaign_name do
        "" -> "draft"
        n -> n
      end

    result =
      case socket.assigns.spec_json do
        {:error, msg} ->
          {:error, msg}

        json ->
          with_spec_file(json, fn path ->
            case run_cli([
                   "runner-pack",
                   "--spec-file",
                   path,
                   "--name",
                   name,
                   "--out-dir",
                   Path.join(repo_root(), ".opencode")
                 ]) do
              {out, 0} ->
                case Jason.decode(out) do
                  {:ok, %{"written" => files}} -> {:ok, files}
                  _ -> {:ok, [String.trim(out)]}
                end

              {out, _} ->
                {:error, last_lines(out)}
            end
          end)
      end

    {:noreply, assign(socket, export_result: result)}
  end

  def handle_event("validate_spec", _params, socket) do
    case socket.assigns.spec_json do
      {:error, msg} ->
        {:noreply, assign(socket, check: {:error, msg})}

      json ->
        {:noreply, assign(socket, check: run_validate(json))}
    end
  end

  def handle_event("create", %{"create" => params}, socket) do
    name = String.trim(params["name"] || "")
    project = String.trim(params["project"] || "")

    cond do
      match?({:error, _}, socket.assigns.spec_json) ->
        {:noreply, assign(socket, create_result: {:error, "fix the graph first"})}

      name == "" or project == "" ->
        {:noreply, assign(socket, create_result: {:error, "name and project are required"})}

      true ->
        {:noreply, assign(socket, create_result: run_create(socket, name, project))}
    end
  end

  defp checkbox_off(data, id, nodes) do
    case Enum.find(nodes, &(&1.id == id)) do
      %{type: :human} -> Map.put(data, "required", false)
      %{type: :intake} -> Map.put(data, "rationale_required", false)
      _ -> data
    end
  end

  defp with_spec_file(json, fun) do
    path =
      Path.join(System.tmp_dir!(), "designer-spec-#{System.unique_integer([:positive])}.json")

    File.write!(path, json)

    try do
      fun.(path)
    after
      File.rm(path)
    end
  end

  defp run_cli(args) do
    [cmd | pre] = String.split(cli_cmd(), " ", trim: true)
    System.cmd(cmd, pre ++ args, stderr_to_stdout: true, cd: repo_root())
  end

  defp run_validate(json) do
    with_spec_file(json, fn path ->
      case run_cli(["validate-spec", "--spec-file", path]) do
        {out, 0} ->
          digest =
            case Jason.decode(out) do
              {:ok, %{"digest" => d}} -> d
              _ -> ""
            end

          {:ok, digest}

        {out, _} ->
          {:error, last_lines(out)}
      end
    end)
  end

  defp run_create(socket, name, project) do
    json =
      case socket.assigns.spec_json do
        json when is_binary(json) -> json
      end

    with_spec_file(json, fn path ->
      case run_cli([
             "create-campaign",
             "--project",
             project,
             "--name",
             name,
             "--kind",
             socket.assigns.kind,
             "--objective",
             "designed in /designer",
             "--spec-file",
             path
           ]) do
        {_, 0} -> {:ok, name}
        {out, _} -> {:error, last_lines(out)}
      end
    end)
  end

  defp last_lines(output) do
    output
    |> String.trim()
    |> String.split("\n")
    |> Enum.take(-2)
    |> Enum.join(" · ")
  end

  defp selected_node(assigns) do
    Enum.find(assigns.nodes, &(&1.id == assigns.selected))
  end

  defp node_meta(type), do: Map.fetch!(@node_style, type)

  defp port_out(%{x: x, y: y}), do: {x + 180, y + 40}
  defp port_in(%{x: x, y: y}), do: {x, y + 44}

  defp wire_path(from, to) do
    {x1, y1} = port_out(from)
    {x2, y2} = port_in(to)
    "M #{x1} #{y1} C #{x1 + 70} #{y1}, #{x2 - 70} #{y2}, #{x2} #{y2}"
  end

  defp node_summary(%{type: :corpus, data: d}), do: d["adapter"]
  defp node_summary(%{type: :intake, data: d}), do: "budget #{d["max_candidates"]}"
  defp node_summary(%{type: :lens, data: d}), do: "#{d["name"]} · #{d["burden"]}"

  defp node_summary(%{type: :human, data: d}),
    do: "#{d["judgement_kind"]} wheel · #{d["quorum"]}"

  defp node_summary(%{type: :rounds, data: d}),
    do: "max #{d["max"]} · #{d["batching"]} batching"

  defp node_summary(%{type: :validation, data: d}), do: d["mode"]
  defp node_summary(%{type: :publish, data: d}), do: "#{d["gate"]} · #{d["granularity"]}"

  defp node_summary(%{type: :runner, data: %{"driver" => "manual"}}),
    do: "manual · humans drive the CLI"

  defp node_summary(%{type: :runner, data: d}),
    do: "#{d["driver"]} · #{d["parallel"]} parallel"

  defp node_detail(%{type: :runner, data: %{"model" => m}}) when m not in [nil, ""], do: m
  defp node_detail(%{type: :rounds, data: d}), do: d["convergence"]
  defp node_detail(%{type: :lens, data: d}), do: d["prompt_ref"]
  defp node_detail(%{type: :human, data: d}), do: "rubric: #{d["rubric"]}"
  defp node_detail(_), do: nil

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:campaigns}
      flash={@flash}
      max_width="max-w-screen-2xl"
      title="Sweep designer"
      subtitle="Compose the review machine as a graph: corpus → intake → panel → rounds → validation → publish. The graph IS the spec."
    >
      <:title_actions>
        <.link navigate="/campaigns" class="btn btn-ghost btn-sm">← Campaigns</.link>
      </:title_actions>

      <div class="space-y-4">
        <section class="app-card p-4" id="designer-toolbar">
          <div class="flex items-end gap-3 flex-wrap">
            <label class="form-control">
              <span class="text-[11px] uppercase tracking-wide opacity-50">
                Kind (loads template)
              </span>
              <form phx-change="set_kind">
                <select name="kind" class="select select-bordered select-sm font-mono">
                  <option
                    :for={k <- ~w(bugsweep perfsweep featuresweep slopsweep)}
                    value={k}
                    selected={k == @kind}
                  >
                    {k}
                  </option>
                </select>
              </form>
            </label>
            <button phx-click="add_corpus" class="btn btn-outline btn-sm" id="add-corpus-button">
              + corpus
            </button>
            <button phx-click="add_lens" class="btn btn-outline btn-sm" id="add-lens-button">
              + lens
            </button>
            <button phx-click="toggle_human" class="btn btn-outline btn-sm" id="toggle-human-button">
              ± human panel
            </button>
            <div class="ml-auto flex items-end gap-2">
              <button phx-click="validate_spec" class="btn btn-outline btn-sm" id="validate-button">
                Validate
              </button>
              <span
                :if={match?({:ok, _}, @check)}
                class="text-xs text-success font-mono"
                id="check-ok"
              >
                ✓ valid · {elem(@check, 1) |> String.slice(0, 12)}
              </span>
            </div>
          </div>
          <div
            :if={match?({:error, _}, @check)}
            class="alert alert-error text-xs mt-2 font-mono"
            id="check-error"
          >
            {elem(@check, 1)}
          </div>
        </section>

        <div class="flex gap-4 items-start">
          <section
            class="rounded-lg overflow-x-auto grow"
            id="designer-canvas-wrap"
            style="background: #17191d;"
          >
            <div
              id="designer-canvas"
              class="relative"
              phx-click="deselect"
              style={"width: 1250px; height: #{canvas_height(@nodes)}px; background-image: radial-gradient(circle, #2a2e35 1px, transparent 1px); background-size: 22px 22px;"}
            >
              <svg
                width="1250"
                height={canvas_height(@nodes)}
                class="absolute inset-0 pointer-events-none"
              >
                <path
                  :for={{from_id, to_id, src_type} <- SweepGraph.edges(@nodes)}
                  d={
                    wire_path(
                      Enum.find(@nodes, &(&1.id == from_id)),
                      Enum.find(@nodes, &(&1.id == to_id))
                    )
                  }
                  stroke={elem(node_meta(src_type), 1)}
                  stroke-width="2"
                  fill="none"
                  opacity="0.85"
                />
              </svg>
              <div
                :for={n <- @nodes}
                id={"node-#{n.id}"}
                phx-hook="NodeDrag"
                phx-click="select_node"
                phx-value-id={n.id}
                data-node-id={n.id}
                class="absolute select-none cursor-pointer"
                style={"left: #{n.x}px; top: #{n.y}px; width: 180px;"}
              >
                <div
                  class="rounded-md shadow-lg overflow-hidden"
                  style={"background: #23262c; border: 1px solid #{if @selected == n.id, do: "#e8e6dc", else: "#31353d"};"}
                >
                  <div
                    data-drag-handle
                    class="px-2.5 py-1 text-[11px] font-semibold tracking-wide cursor-grab"
                    style={"background: #{elem(node_meta(n.type), 1)}; color: #f4f2ea;"}
                  >
                    {elem(node_meta(n.type), 0)}
                  </div>
                  <div class="px-2.5 py-2 text-xs font-mono" style="color: #cfd3c9;">
                    {node_summary(n)}
                    <div :if={node_detail(n)} class="opacity-50 truncate">{node_detail(n)}</div>
                  </div>
                </div>
                <span
                  :if={n.type != :corpus}
                  class="absolute rounded-full"
                  style="left: -4px; top: 36px; width: 8px; height: 8px; background: #8a9097;"
                />
                <span
                  :if={n.type != :publish}
                  class="absolute rounded-full"
                  style={"right: -4px; top: 36px; width: 8px; height: 8px; background: #{elem(node_meta(n.type), 1)};"}
                />
              </div>
            </div>
          </section>

          <aside class="app-card p-4 w-64 shrink-0" id="designer-sidebar">
            <%= if node = selected_node(assigns) do %>
              <div class="flex items-baseline gap-2 mb-3">
                <h2 class="text-xs uppercase tracking-widest opacity-60">
                  {elem(node_meta(node.type), 0)}
                </h2>
                <span class="text-[11px] font-mono opacity-40">{node.id}</span>
                <button
                  :if={node.type in [:lens, :corpus, :human]}
                  phx-click="remove_node"
                  phx-value-id={node.id}
                  class="btn btn-ghost btn-xs ml-auto text-error"
                  id="remove-node-button"
                >
                  remove
                </button>
              </div>
              <form phx-change="update_node" class="space-y-2" id={"node-editor-#{node.id}"}>
                <input type="hidden" name="node[id]" value={node.id} />
                <.node_fields node={node} export_result={@export_result} />
              </form>
            <% else %>
              <p class="text-sm opacity-60">
                Click a node to edit it. Drag by the title bar to rearrange.
                Wires are derived from the pipeline — a sweep can't be
                mis-wired, only mis-configured (and Validate catches that).
              </p>
            <% end %>

            <div class="border-t border-base-200 mt-4 pt-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60 mb-2">Create campaign</h2>
              <form
                phx-submit="create"
                phx-change="create_meta"
                class="space-y-2"
                id="designer-create-form"
              >
                <input
                  type="text"
                  name="create[name]"
                  value={@campaign_name}
                  placeholder="campaign name"
                  class="input input-bordered input-sm w-full font-mono"
                />
                <select name="create[project]" class="select select-bordered select-sm w-full">
                  <option value="">Pick a project</option>
                  <option :for={p <- @projects} value={p.name} selected={p.name == @project}>
                    {p.name}
                  </option>
                </select>
                <button
                  type="submit"
                  class="btn btn-primary btn-sm w-full"
                  id="designer-create-button"
                >
                  Create from graph
                </button>
              </form>
              <p
                :if={match?({:ok, _}, @create_result)}
                class="text-xs text-success mt-2"
                id="create-ok"
              >
                Created —
                <.link navigate={"/campaigns/#{elem(@create_result, 1)}"} class="link">
                  open {elem(@create_result, 1)}
                </.link>
              </p>
              <p
                :if={match?({:error, _}, @create_result)}
                class="text-xs text-error mt-2 font-mono"
                id="create-error"
              >
                {elem(@create_result, 1)}
              </p>
            </div>
          </aside>
        </div>

        <section class="app-card p-4" id="designer-spec">
          <details>
            <summary class="text-xs uppercase tracking-widest opacity-60 cursor-pointer">
              Compiled SweepSpec JSON
            </summary>
            <%= case @spec_json do %>
              <% {:error, msg} -> %>
                <div class="alert alert-error text-xs mt-2 font-mono">{msg}</div>
              <% json -> %>
                <pre class="text-xs font-mono overflow-x-auto mt-2 opacity-80">{json}</pre>
            <% end %>
          </details>
        </section>
      </div>
    </.workspace_page>
    """
  end

  defp canvas_height(nodes) do
    max(560, Enum.max(Enum.map(nodes, & &1.y)) + 180)
  end

  attr :node, :map, required: true
  attr :export_result, :any, default: nil

  defp node_fields(%{node: %{type: :corpus}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Adapter</span>
      <select name="node[adapter]" class="select select-bordered select-sm font-mono">
        <option :for={a <- SweepGraph.adapters()} value={a} selected={a == @node.data["adapter"]}>
          {a}
        </option>
      </select>
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Config JSON</span>
      <textarea
        name="node[config_raw]"
        rows="6"
        class="textarea textarea-bordered textarea-sm font-mono w-full"
      >{@node.data["config_raw"]}</textarea>
    </label>
    """
  end

  defp node_fields(%{node: %{type: :intake}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Candidate budget</span>
      <input
        type="number"
        name="node[max_candidates]"
        value={@node.data["max_candidates"]}
        min="1"
        class="input input-bordered input-sm font-mono"
      />
    </label>
    <label class="flex items-center gap-2 text-sm">
      <input
        type="checkbox"
        name="node[rationale_required]"
        checked={@node.data["rationale_required"]}
        class="checkbox checkbox-sm"
      /> rationale required
    </label>
    """
  end

  defp node_fields(%{node: %{type: :lens}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Name</span>
      <input
        type="text"
        name="node[name]"
        value={@node.data["name"]}
        class="input input-bordered input-sm font-mono"
      />
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Prompt ref</span>
      <input
        type="text"
        name="node[prompt_ref]"
        value={@node.data["prompt_ref"]}
        list="lens-prompts"
        class="input input-bordered input-sm font-mono"
      />
      <datalist id="lens-prompts">
        <option value="lens:root-cause" />
        <option value="lens:lifecycle-regression" />
        <option value="lens:perf-budget" />
        <option value="lens:spec-honesty" />
        <option value="lens:fake-success" />
        <option value="lens:slop" />
      </datalist>
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Burden</span>
      <select name="node[burden]" class="select select-bordered select-sm font-mono">
        <option value="refute" selected={@node.data["burden"] == "refute"}>refute</option>
        <option value="confirm" selected={@node.data["burden"] == "confirm"}>confirm</option>
      </select>
    </label>
    """
  end

  defp node_fields(%{node: %{type: :human}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Rubric (EvalTemplate)</span>
      <input
        type="text"
        name="node[rubric]"
        value={@node.data["rubric"]}
        class="input input-bordered input-sm font-mono"
      />
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Judgement kind</span>
      <select name="node[judgement_kind]" class="select select-bordered select-sm font-mono">
        <option value="single" selected={@node.data["judgement_kind"] == "single"}>single</option>
        <option value="pair" selected={@node.data["judgement_kind"] == "pair"}>pair</option>
      </select>
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Quorum</span>
      <select name="node[quorum]" class="select select-bordered select-sm font-mono">
        <option value="all_lenses_and_human" selected={@node.data["quorum"] == "all_lenses_and_human"}>
          all_lenses_and_human
        </option>
        <option value="all_lenses" selected={@node.data["quorum"] == "all_lenses"}>all_lenses</option>
      </select>
    </label>
    <label class="flex items-center gap-2 text-sm">
      <input
        type="checkbox"
        name="node[required]"
        checked={@node.data["required"]}
        class="checkbox checkbox-sm"
      /> human verdict required
    </label>
    """
  end

  defp node_fields(%{node: %{type: :rounds}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Max rounds (hard cap)</span>
      <input
        type="number"
        name="node[max]"
        value={@node.data["max"]}
        min="1"
        class="input input-bordered input-sm font-mono"
      />
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Batching</span>
      <select name="node[batching]" class="select select-bordered select-sm font-mono">
        <option value="required" selected={@node.data["batching"] == "required"}>required</option>
        <option value="none" selected={@node.data["batching"] == "none"}>none</option>
      </select>
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Convergence</span>
      <select name="node[convergence]" class="select select-bordered select-sm font-mono">
        <option
          value="no_new_confirmed_findings"
          selected={@node.data["convergence"] == "no_new_confirmed_findings"}
        >
          no_new_confirmed_findings
        </option>
        <option
          value="all_findings_settled"
          selected={@node.data["convergence"] == "all_findings_settled"}
        >
          all_findings_settled
        </option>
      </select>
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Repair depth (per REFUTE)</span>
      <select name="node[repair]" class="select select-bordered select-sm font-mono">
        <option value="1" selected={to_string(@node.data["repair"]) == "1"}>
          1 — one repair cycle
        </option>
        <option value="0" selected={to_string(@node.data["repair"]) == "0"}>
          0 — repairs forbidden
        </option>
      </select>
    </label>
    """
  end

  defp node_fields(%{node: %{type: :validation}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Mode</span>
      <select name="node[mode]" class="select select-bordered select-sm font-mono">
        <option value="red_green" selected={@node.data["mode"] == "red_green"}>red_green</option>
        <option value="perf_budget" selected={@node.data["mode"] == "perf_budget"}>
          perf_budget
        </option>
        <option value="rubric_only" selected={@node.data["mode"] == "rubric_only"}>
          rubric_only
        </option>
      </select>
    </label>
    <label :if={@node.data["mode"] == "perf_budget"} class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Perf budgets JSON</span>
      <textarea
        name="node[perf_budgets_raw]"
        rows="6"
        class="textarea textarea-bordered textarea-sm font-mono w-full"
      >{@node.data["perf_budgets_raw"]}</textarea>
    </label>
    """
  end

  defp node_fields(%{node: %{type: :runner}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Driver</span>
      <select name="node[driver]" class="select select-bordered select-sm font-mono">
        <option value="manual" selected={@node.data["driver"] == "manual"}>
          manual — humans drive the CLI
        </option>
        <option value="opencode" selected={@node.data["driver"] == "opencode"}>opencode</option>
        <option value="claude-workflow" selected={@node.data["driver"] == "claude-workflow"}>
          claude-workflow
        </option>
      </select>
    </label>
    <label :if={@node.data["driver"] != "manual"} class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Model (optional pin)</span>
      <input
        type="text"
        name="node[model]"
        value={@node.data["model"]}
        placeholder="e.g. anthropic/claude-sonnet-5"
        class="input input-bordered input-sm font-mono"
      />
    </label>
    <label :if={@node.data["driver"] != "manual"} class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Parallel lens workers</span>
      <input
        type="number"
        name="node[parallel]"
        value={@node.data["parallel"]}
        min="1"
        class="input input-bordered input-sm font-mono"
      />
    </label>
    <p class="text-xs opacity-55">
      The runner is hands, never judgment — round guards (batching, cap,
      convergence, dispositions) stay enforced by the campaign CLI.
    </p>
    <button
      :if={@node.data["driver"] == "opencode"}
      type="button"
      phx-click="export_pack"
      class="btn btn-outline btn-sm w-full"
      id="export-pack-button"
    >
      Export opencode pack
    </button>
    <div :if={match?({:ok, _}, @export_result)} class="text-xs font-mono mt-1" id="export-ok">
      <p class="text-success">pack written:</p>
      <p :for={f <- elem(@export_result, 1)} class="opacity-70 truncate">{f}</p>
    </div>
    <p
      :if={match?({:error, _}, @export_result)}
      class="text-xs text-error font-mono mt-1"
      id="export-error"
    >
      {elem(@export_result, 1)}
    </p>
    """
  end

  defp node_fields(%{node: %{type: :publish}} = assigns) do
    ~H"""
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Gate</span>
      <select name="node[gate]" class="select select-bordered select-sm font-mono">
        <option value="human" selected={@node.data["gate"] == "human"}>human</option>
        <option value="none" selected={@node.data["gate"] == "none"}>none</option>
      </select>
    </label>
    <label class="form-control">
      <span class="text-[11px] uppercase tracking-wide opacity-50">Granularity</span>
      <select name="node[granularity]" class="select select-bordered select-sm font-mono">
        <option
          value="branch-per-finding"
          selected={@node.data["granularity"] == "branch-per-finding"}
        >
          branch-per-finding
        </option>
        <option value="pr-per-finding" selected={@node.data["granularity"] == "pr-per-finding"}>
          pr-per-finding
        </option>
        <option value="report-only" selected={@node.data["granularity"] == "report-only"}>
          report-only
        </option>
      </select>
    </label>
    """
  end
end
