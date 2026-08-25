defmodule TournamentUiWeb.InspectLive do
  @moduledoc """
  /inspect — kitchen-sink data viewer.

  Tabs across the SQLite tables (domains, pending, scores) plus Langfuse
  prompts. Per-tab filters (domain name, status, run, rater_type), sortable
  columns, expand-row-to-JSON, download-as-CSV-or-JSON for the current view.

  Read-only. /results remains the reviewer-facing comparison view.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.Inspect, as: Data
  alias TournamentUi.LangfusePrompts

  @entities [:domains, :pending, :scores, :prompts]

  @impl true
  def mount(_params, _session, socket) do
    {:ok,
     socket
     |> assign(
       entity: :domains,
       filters: %{domain: nil, status: nil, run: nil, rater_type: nil},
       sort_col: nil,
       sort_dir: :asc,
       expanded_id: nil,
       prompts: [],
       prompt_backend: LangfusePrompts.backend_info(),
       prompts_loading?: false
     )
     |> load_data()}
  end

  @impl true
  def handle_event("select_entity", %{"entity" => e}, socket) do
    entity = String.to_existing_atom(e)

    socket =
      socket
      |> assign(entity: entity, expanded_id: nil, sort_col: nil, sort_dir: :asc)
      |> load_data()

    {:noreply, socket}
  end

  def handle_event("filter_domain", %{"domain" => v}, socket) do
    {:noreply,
     socket
     |> update(:filters, &Map.put(&1, :domain, normalize(v)))
     |> load_data()}
  end

  def handle_event("filter_status", %{"status" => v}, socket) do
    {:noreply,
     socket
     |> update(:filters, &Map.put(&1, :status, normalize(v)))
     |> load_data()}
  end

  def handle_event("filter_run", %{"run" => v}, socket) do
    {:noreply,
     socket
     |> update(:filters, &Map.put(&1, :run, normalize(v)))
     |> load_data()}
  end

  def handle_event("filter_rater", %{"rater_type" => v}, socket) do
    {:noreply,
     socket
     |> update(:filters, &Map.put(&1, :rater_type, normalize(v)))
     |> load_data()}
  end

  def handle_event("sort", %{"col" => col}, socket) do
    col_atom = String.to_atom(col)

    {sort_col, sort_dir} =
      case socket.assigns.sort_col do
        ^col_atom when socket.assigns.sort_dir == :asc -> {col_atom, :desc}
        ^col_atom -> {nil, :asc}
        _ -> {col_atom, :asc}
      end

    rows =
      socket.assigns.rows
      |> sort_rows(sort_col, sort_dir)

    {:noreply, assign(socket, rows: rows, sort_col: sort_col, sort_dir: sort_dir)}
  end

  def handle_event("expand_row", %{"id" => id}, socket) do
    eid = if to_string(socket.assigns.expanded_id) == id, do: nil, else: id
    {:noreply, assign(socket, expanded_id: eid)}
  end

  def handle_event("clear_filters", _params, socket) do
    {:noreply,
     socket
     |> assign(filters: %{domain: nil, status: nil, run: nil, rater_type: nil})
     |> load_data()}
  end

  defp normalize(""), do: nil
  defp normalize(v), do: v

  defp load_data(socket) do
    rows =
      case socket.assigns.entity do
        :domains -> Data.domains()
        :pending -> Data.pending(socket.assigns.filters)
        :scores -> Data.scores(socket.assigns.filters)
        :prompts -> []
      end

    socket
    |> assign(rows: rows, counts: Data.counts())
    |> maybe_load_prompts()
  end

  defp maybe_load_prompts(%{assigns: %{entity: :prompts, prompts: []}} = socket) do
    # Load prompts on first visit; cache in socket assigns
    assign(socket,
      prompts: LangfusePrompts.list(),
      prompt_backend: LangfusePrompts.backend_info()
    )
  end

  defp maybe_load_prompts(socket), do: socket

  defp sort_rows(rows, nil, _), do: rows

  defp sort_rows(rows, col, dir) do
    sorted = Enum.sort_by(rows, fn r -> Map.get(r, col) end, sort_compare(dir))
    sorted
  end

  defp sort_compare(:asc), do: &<=/2
  defp sort_compare(:desc), do: &>=/2

  # ── Render ─────────────────────────────────────────────────────────────

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:inspect}
      max_width="max-w-7xl"
      title="Data inspector"
      subtitle="Advanced, read-only troubleshooting for raw domains, queue rows, scores, and prompt metadata."
    >
      <:title_actions>
        <div class="text-xs font-mono opacity-70 flex gap-3 flex-wrap">
          <span>domains: <strong>{@counts.domains}</strong></span>
          <span>pending: <strong>{@counts.pending}</strong></span>
          <span>scores: <strong>{@counts.scores}</strong></span>
        </div>
      </:title_actions>

      <div class="app-card px-4 py-3 mb-4 flex items-center justify-between gap-4 text-sm">
        <span class="opacity-70">Looking for human and model outcomes rather than raw rows?</span>
        <.link navigate="/results" class="btn btn-sm btn-ghost">Open Results →</.link>
      </div>
      <div class="tabs tabs-boxed mb-4 inline-flex">
        <%= for e <- @entities_list do %>
          <button
            phx-click="select_entity"
            phx-value-entity={e}
            class={["tab", @entity == String.to_atom(e) && "tab-active"]}
          >
            {e}
          </button>
        <% end %>
      </div>
      <div class="app-card p-4 mb-4 flex flex-wrap items-end gap-3 text-sm">
        <%= if @entity in [:pending, :scores] do %>
          <label class="flex flex-col gap-1">
            <span class="text-xs uppercase tracking-wider opacity-60">Domain</span>
            <select
              phx-change="filter_domain"
              name="domain"
              class="select select-sm select-bordered min-w-[12rem]"
            >
              <option value="">— all —</option>
              <%= for n <- @domain_names do %>
                <option value={n} selected={@filters.domain == n}>{n}</option>
              <% end %>
            </select>
          </label>
        <% end %>

        <%= if @entity == :pending do %>
          <label class="flex flex-col gap-1">
            <span class="text-xs uppercase tracking-wider opacity-60">Status</span>
            <select phx-change="filter_status" name="status" class="select select-sm select-bordered">
              <option value="">— all —</option>
              <%= for s <- ~w(pending done error cancelled) do %>
                <option value={s} selected={@filters.status == s}>{s}</option>
              <% end %>
            </select>
          </label>

          <label class="flex flex-col gap-1">
            <span class="text-xs uppercase tracking-wider opacity-60">Run</span>
            <select
              phx-change="filter_run"
              name="run"
              class="select select-sm select-bordered min-w-[14rem]"
            >
              <option value="">— all —</option>
              <%= for r <- @runs do %>
                <option value={r} selected={@filters.run == r}>{r}</option>
              <% end %>
            </select>
          </label>
        <% end %>

        <%= if @entity == :scores do %>
          <label class="flex flex-col gap-1">
            <span class="text-xs uppercase tracking-wider opacity-60">Rater</span>
            <select
              phx-change="filter_rater"
              name="rater_type"
              class="select select-sm select-bordered"
            >
              <option value="">— all —</option>
              <%= for r <- ~w(human llm) do %>
                <option value={r} selected={@filters.rater_type == r}>{r}</option>
              <% end %>
            </select>
          </label>
        <% end %>

        <%= if has_active_filter?(@filters) do %>
          <button phx-click="clear_filters" class="btn btn-ghost btn-sm">clear filters</button>
        <% end %>

        <div class="ml-auto flex gap-2">
          <.link
            href={download_url(@entity, "json", @filters)}
            class="btn btn-sm btn-ghost"
          >
            ↓ JSON
          </.link>
          <.link
            href={download_url(@entity, "csv", @filters)}
            class="btn btn-sm btn-ghost"
          >
            ↓ CSV
          </.link>
        </div>
      </div>
      <%= cond do %>
        <% @entity == :prompts -> %>
          <div class="text-xs opacity-60 mb-3">
            Showing prompt metadata from <strong>{@prompt_backend.label}</strong>
            · <span class="font-mono">{@prompt_backend.location}</span>
          </div>
          <.prompts_table prompts={@prompts} expanded_id={@expanded_id} />
        <% @rows == [] -> %>
          <div class="app-card p-8 text-center text-sm opacity-70">
            No rows match the current filters.
          </div>
        <% true -> %>
          <.entity_table
            entity={@entity}
            rows={@rows}
            sort_col={@sort_col}
            sort_dir={@sort_dir}
            expanded_id={@expanded_id}
          />
      <% end %>
    </.workspace_page>
    """
  end

  attr :entity, :atom, required: true
  attr :rows, :list, required: true
  attr :sort_col, :any, required: true
  attr :sort_dir, :atom, required: true
  attr :expanded_id, :any, required: true

  defp entity_table(assigns) do
    assigns = assign(assigns, :columns, columns_for(assigns.entity))

    ~H"""
    <div class="app-card overflow-x-auto">
      <table class="table table-sm">
        <thead>
          <tr>
            <th class="w-8"></th>
            <%= for {col, label} <- @columns do %>
              <th class="cursor-pointer select-none" phx-click="sort" phx-value-col={col}>
                {label}
                <%= if @sort_col == String.to_atom(col) do %>
                  <span class="opacity-60">{if @sort_dir == :asc, do: "▲", else: "▼"}</span>
                <% end %>
              </th>
            <% end %>
          </tr>
        </thead>
        <tbody>
          <%= for row <- @rows do %>
            <tr
              class="hover:bg-base-200 cursor-pointer"
              phx-click="expand_row"
              phx-value-id={row[:id]}
            >
              <td class="text-xs opacity-40">
                <%= if to_string(row[:id]) == to_string(@expanded_id) do %>
                  ▾
                <% else %>
                  ▸
                <% end %>
              </td>
              <%= for {col, _label} <- @columns do %>
                <td class="font-mono text-xs whitespace-nowrap max-w-[24rem] truncate">
                  {format_cell(row[String.to_atom(col)])}
                </td>
              <% end %>
            </tr>
            <%= if to_string(row[:id]) == to_string(@expanded_id) do %>
              <tr class="bg-base-200">
                <td colspan={length(@columns) + 1}>
                  <pre class="whitespace-pre-wrap text-xs leading-5 p-3 bg-base-100 rounded font-mono"><%= Jason.encode!(row, pretty: true) %></pre>
                </td>
              </tr>
            <% end %>
          <% end %>
        </tbody>
      </table>
    </div>
    """
  end

  attr :prompts, :list, required: true
  attr :expanded_id, :any, required: true

  defp prompts_table(assigns) do
    ~H"""
    <%= if @prompts == [] do %>
      <div class="app-card p-8 text-center text-sm opacity-70">
        No prompts found in Langfuse, or Langfuse is unreachable.
      </div>
    <% else %>
      <div class="app-card overflow-x-auto">
        <table class="table table-sm">
          <thead>
            <tr>
              <th>name</th>
              <th>versions</th>
              <th>production</th>
              <th>candidate</th>
              <th>labels</th>
            </tr>
          </thead>
          <tbody>
            <%= for p <- @prompts do %>
              <tr>
                <td class="font-mono text-xs">{p.name}</td>
                <td class="font-mono text-xs">{length(p.all_versions)}</td>
                <td class="font-mono text-xs">{p.production_version || "—"}</td>
                <td class="font-mono text-xs">{p.candidate_version || "—"}</td>
                <td class="font-mono text-xs">{Enum.join(MapSet.to_list(p.labels), ", ")}</td>
              </tr>
            <% end %>
          </tbody>
        </table>
      </div>
    <% end %>
    """
  end

  # ── Helpers ────────────────────────────────────────────────────────────

  defp columns_for(:domains),
    do: [
      {"id", "id"},
      {"name", "name"},
      {"description", "description"},
      {"rubric", "rubric"},
      {"status", "status"},
      {"created_at", "created"}
    ]

  defp columns_for(:pending),
    do: [
      {"id", "id"},
      {"domain_name", "domain"},
      {"match_id", "match"},
      {"status", "status"},
      {"tournament_db_path", "run"},
      {"created_at", "created"}
    ]

  defp columns_for(:scores),
    do: [
      {"id", "id"},
      {"domain_name", "domain"},
      {"name", "name"},
      {"value", "value"},
      {"rater_type", "rater"},
      {"created_at", "created"}
    ]

  defp columns_for(_), do: []

  defp format_cell(nil), do: "—"

  defp format_cell(v) when is_binary(v) do
    if String.length(v) > 80, do: String.slice(v, 0, 77) <> "…", else: v
  end

  defp format_cell(v) when is_map(v) or is_list(v) do
    "{…}"
  end

  defp format_cell(v), do: to_string(v)

  defp has_active_filter?(filters) do
    Enum.any?(filters, fn {_k, v} -> v not in [nil, ""] end)
  end

  defp download_url(entity, fmt, filters) do
    qs =
      filters
      |> Enum.reject(fn {_, v} -> v in [nil, ""] end)
      |> Enum.map(fn {k, v} -> "#{k}=#{URI.encode(to_string(v))}" end)
      |> case do
        [] -> ""
        parts -> "&" <> Enum.join(parts, "&")
      end

    "/inspect/download?entity=#{entity}&fmt=#{fmt}#{qs}"
  end

  # Make these available as assigns in render/1
  @impl true
  def handle_params(_params, _uri, socket) do
    {:noreply,
     assign(socket,
       entities_list: Enum.map(@entities, &Atom.to_string/1),
       domain_names: Data.domain_names(),
       runs: Data.runs()
     )}
  end
end
