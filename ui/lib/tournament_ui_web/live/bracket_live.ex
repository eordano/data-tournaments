defmodule TournamentUiWeb.BracketLive do
  use TournamentUiWeb, :live_view

  alias TournamentUi.{Tournament, Config, Optimizer, Runner}
  alias TournamentUiWeb.SafeMarkdown

  @impl true
  def mount(_params, _session, socket) do
    entries = list_entries()
    configs = Config.list()

    if connected?(socket), do: :timer.send_interval(3_000, :refresh)

    selected = List.first(entries)
    bracket = load_bracket(selected)

    {:ok,
     assign(socket,
       entries: entries,
       configs: configs,
       selected: selected,
       bracket: bracket,
       focus_match: nil,
       optimize_open: false,
       optimize_state: :idle,
       optimize_result: nil,
       optimize_error: nil,
       critique: ""
     )}
  end

  @impl true
  def handle_params(%{"select" => name}, _uri, socket) do
    entry = Enum.find(socket.assigns.entries, &(&1.name == name))

    if entry do
      {:noreply, assign(socket, selected: entry, bracket: load_bracket(entry))}
    else
      {:noreply, socket}
    end
  end

  def handle_params(_params, _uri, socket), do: {:noreply, socket}

  @impl true
  def handle_event("select_entry", %{"name" => name}, socket) do
    entry = Enum.find(socket.assigns.entries, &(&1.name == name))
    bracket = load_bracket(entry)
    {:noreply, assign(socket, selected: entry, bracket: bracket, focus_match: nil)}
  end

  def handle_event("start_run", %{"name" => name} = params, socket) do
    fresh = params["fresh"] == "true"

    case Enum.find(socket.assigns.configs, &(&1.name == name)) do
      nil ->
        {:noreply, put_flash(socket, :error, "no config named #{name}")}

      config ->
        Runner.start(config.path, fresh: fresh)

        entries = list_entries()
        selected = Enum.find(entries, &(&1.name == name))
        msg = if fresh, do: "restarted #{name} (fresh)", else: "started #{name}"

        {:noreply,
         socket
         |> assign(entries: entries, selected: selected)
         |> put_flash(:info, msg)}
    end
  end

  def handle_event("focus_match", %{"id" => id}, socket) do
    match =
      case socket.assigns.selected do
        %{path: p} when is_binary(p) -> Tournament.match(p, String.to_integer(id))
        _ -> nil
      end

    {:noreply, assign(socket, focus_match: match)}
  end

  def handle_event("close_match", _, socket) do
    {:noreply, assign(socket, focus_match: nil)}
  end

  def handle_event("open_optimize", _, socket) do
    {:noreply,
     assign(socket,
       optimize_open: true,
       optimize_state: :idle,
       optimize_result: nil,
       optimize_error: nil
     )}
  end

  def handle_event("close_optimize", _, socket) do
    {:noreply, assign(socket, optimize_open: false)}
  end

  def handle_event("update_critique", %{"critique" => c}, socket) do
    {:noreply, assign(socket, critique: c)}
  end

  def handle_event("run_optimize", _params, socket) do
    config =
      Enum.find(socket.assigns.configs, fn c ->
        c.name == socket.assigns.selected.name or
          String.starts_with?(socket.assigns.selected.name, c.name)
      end)

    cond do
      is_nil(config) ->
        {:noreply,
         assign(socket, optimize_error: "no matching config for #{socket.assigns.selected.name}")}

      true ->
        parent = self()
        samples = Tournament.sample_conclusions(socket.assigns.selected.path, 3)
        critique = socket.assigns.critique
        current = config.json["match_prompt"] || ""

        Task.start(fn ->
          result = Optimizer.optimize(current, samples, critique)
          send(parent, {:optimize_done, config.path, result})
        end)

        {:noreply, assign(socket, optimize_state: :running, optimize_error: nil)}
    end
  end

  def handle_event("apply_improved", _params, socket) do
    case socket.assigns.optimize_result do
      %{improved: improved, config_path: path} when is_binary(improved) and improved != "" ->
        case Config.update_prompt(path, improved) do
          :ok ->
            configs = Config.list()

            {:noreply,
             socket
             |> assign(configs: configs, optimize_state: :applied)
             |> put_flash(:info, "prompt updated in #{Path.basename(path)}")}

          {:error, reason} ->
            {:noreply, assign(socket, optimize_error: "write failed: #{inspect(reason)}")}
        end

      _ ->
        {:noreply, assign(socket, optimize_error: "no improved prompt to apply")}
    end
  end

  @impl true
  def handle_info(:refresh, socket) do
    entries = list_entries()
    configs = Config.list()

    selected =
      case socket.assigns.selected do
        nil -> List.first(entries)
        s -> Enum.find(entries, &(&1.name == s.name)) || s
      end

    bracket =
      if selected && selected.has_db? do
        load_bracket(selected)
      else
        socket.assigns.bracket
      end

    {:noreply,
     assign(socket, entries: entries, configs: configs, selected: selected, bracket: bracket)}
  end

  def handle_info({:optimize_done, config_path, {:ok, result}}, socket) do
    {:noreply,
     assign(socket,
       optimize_state: :done,
       optimize_result: Map.put(result, :config_path, config_path)
     )}
  end

  def handle_info({:optimize_done, _, {:error, reason}}, socket) do
    {:noreply, assign(socket, optimize_state: :error, optimize_error: reason)}
  end

  # ── Render ───────────────────────────────────────────────────────────

  @impl true
  def render(assigns) do
    ~H"""
    <div class="flex flex-col h-screen bg-base-200">
      <div class="flex items-center justify-between px-6 py-2 bg-base-100 border-b app-hairline shrink-0">
        <.workspace_nav current={:brackets} />
        <div class="flex items-center gap-3 text-xs">
          <span class="opacity-55" id="brackets-legacy-note">
            advanced/legacy — normal entry is Start → generate → judge
          </span>
          <.link navigate="/start" class="font-medium hover:text-primary">Start →</.link>
        </div>
      </div>
      <div class="flex flex-1 overflow-hidden">
        <aside class="w-72 shrink-0 bg-base-100 border-r app-hairline flex flex-col overflow-hidden">
          <div class="px-4 pt-4 pb-3 flex items-center justify-between">
            <div>
              <h2 class="font-semibold text-base leading-tight">Direct brackets</h2>
              <p class="text-xs opacity-60 mt-0.5">advanced · prepared artifacts</p>
            </div>
            <.link navigate="/new" class="btn btn-sm btn-primary gap-1">
              <span aria-hidden="true">+</span>
              <span>new</span>
            </.link>
          </div>

          <div class="flex-1 overflow-y-auto px-2 pb-4 space-y-1">
            <%= for e <- @entries do %>
              <div class={[
                "entry-row",
                @selected && @selected.name == e.name && "is-selected"
              ]}>
                <button
                  phx-click="select_entry"
                  phx-value-name={e.name}
                  class="w-full text-left px-3 py-2.5 cursor-pointer"
                >
                  <div class="flex items-center gap-2">
                    <div class="font-medium truncate flex-1 text-sm">{e.name}</div>
                    <span class={status_badge_class(e.status)}>{status_label(e.status)}</span>
                  </div>
                  <div class="text-xs opacity-60 mt-1">
                    <%= cond do %>
                      <% e.progress && e.status == :running -> %>
                        Round {e.progress.round} · {e.progress.done}/{e.progress.total}
                      <% e.has_db? -> %>
                        {e.rounds} rounds · {e.matches} matches
                      <% true -> %>
                        not started
                    <% end %>
                  </div>
                </button>
                <%= if e.status == :pending do %>
                  <button
                    phx-click="start_run"
                    phx-value-name={e.name}
                    class="block w-full text-xs font-medium text-success hover:bg-success/10 px-3 py-1.5 border-t app-hairline transition cursor-pointer"
                  >
                    ▶ start run
                  </button>
                <% end %>
              </div>
            <% end %>
            <%= if @entries == [] do %>
              <div class="mx-2 mt-6 p-4 rounded-lg border border-dashed app-hairline text-center">
                <div class="text-sm font-medium opacity-80">No direct brackets yet</div>
                <div class="text-xs opacity-60 mt-1">
                  Click <span class="font-semibold">+ new</span> to create one.
                </div>
              </div>
            <% end %>
          </div>
        </aside>

        <main class="flex-1 flex flex-col overflow-hidden workspace">
          <header class="flex items-center justify-between px-6 py-4 bg-base-100/80 backdrop-blur border-b app-hairline">
            <div class="min-w-0 flex-1">
              <h1 class="text-xl font-semibold truncate">
                {if @selected, do: @selected.name, else: "(no selection)"}
              </h1>
              <p class="text-sm opacity-70 flex items-center gap-2 mt-0.5">
                <%= if @selected do %>
                  <span class={status_badge_class(@selected.status)}>
                    {status_label(@selected.status)}
                  </span>
                  <%= if Map.get(@selected, :progress) do %>
                    <span class="font-mono text-xs">
                      R{@selected.progress.round} · {@selected.progress.done}/{@selected.progress.total}
                    </span>
                  <% end %>
                  <span class="truncate text-xs font-mono opacity-60">
                    {@selected.path || "(no DB yet)"}
                  </span>
                <% else %>
                  <span class="text-xs opacity-60">Pick a direct bracket from the sidebar.</span>
                <% end %>
              </p>
            </div>
            <%= if @selected do %>
              <div class="flex gap-2 shrink-0 ml-4">
                <%= if @selected.status == :pending do %>
                  <button
                    phx-click="start_run"
                    phx-value-name={@selected.name}
                    class="btn btn-success btn-sm gap-1"
                  >
                    <span aria-hidden="true">▶</span> start run
                  </button>
                <% end %>
                <button
                  phx-click="open_optimize"
                  class="btn btn-primary btn-sm"
                  disabled={!@selected.has_db?}
                >
                  Optimize prompt
                </button>
              </div>
            <% end %>
          </header>

          <div class="flex-1 overflow-auto p-6">
            <%= cond do %>
              <% @bracket -> %>
                <div class="flex gap-8 items-start">
                  <%= for {round, matches} <- @bracket.rounds do %>
                    <div class="flex flex-col gap-3 min-w-64">
                      <div class="font-semibold text-xs uppercase tracking-widest opacity-60">
                        Round {round}
                      </div>
                      <%= for m <- matches do %>
                        <button
                          phx-click="focus_match"
                          phx-value-id={m.id}
                          class={match_card_class(m)}
                        >
                          <div class="text-[10px] font-mono uppercase tracking-wider opacity-60 mb-1">
                            R{m.round}-{m.slot + 1}{if m.is_bye, do: " · bye", else: ""}
                          </div>
                          <div class="text-sm font-medium truncate">{m.title}</div>
                          <div class="text-xs opacity-60 mt-1 truncate">
                            {input_label(m.input_a)}
                            <%= if m.input_b do %>
                              <span class="opacity-40">vs</span> {input_label(m.input_b)}
                            <% end %>
                          </div>
                        </button>
                      <% end %>
                    </div>
                  <% end %>
                </div>
              <% @selected && @selected.status == :running -> %>
                <div class="mx-auto mt-16 max-w-md text-center app-card p-8">
                  <div class="loading loading-spinner loading-lg mb-3 text-primary"></div>
                  <div class="font-medium">Direct bracket is running…</div>
                  <div class="text-xs opacity-60 mt-2 font-mono">
                    tail /tmp/harness/runs/{@selected.name}.log
                  </div>
                </div>
              <% @selected && @selected.status == :pending -> %>
                <div class="mx-auto mt-16 max-w-md text-center app-card p-8">
                  <div class="text-sm opacity-70 mb-4">Config ready — not yet started.</div>
                  <button
                    phx-click="start_run"
                    phx-value-name={@selected.name}
                    class="btn btn-success btn-lg gap-2"
                  >
                    <span aria-hidden="true">▶</span> start run
                  </button>
                </div>
              <% true -> %>
                <div class="mx-auto mt-16 max-w-md text-center app-card p-8">
                  <div class="text-sm opacity-70">
                    Select a direct bracket from the sidebar, or click
                    <span class="font-semibold">+ new</span>
                    to create one.
                  </div>
                </div>
            <% end %>
          </div>
        </main>

        <%= if @focus_match do %>
          <div
            class="fixed inset-0 modal-overlay flex items-center justify-center z-40 p-4"
            phx-click="close_match"
          >
            <div
              class="modal-panel max-w-3xl w-full max-h-[80vh] overflow-auto"
              phx-click-away="close_match"
              phx-click="ignore"
            >
              <div class="px-5 py-3 border-b app-hairline flex justify-between items-center">
                <h3 class="font-semibold">
                  R{@focus_match.round}-{@focus_match.slot + 1} — {@focus_match.title}
                </h3>
                <button phx-click="close_match" class="btn btn-sm btn-ghost">Close</button>
              </div>
              <div class="px-6 py-5 prose prose-sm max-w-none">
                <%= if @focus_match.conclusion do %>
                  {SafeMarkdown.render(@focus_match.conclusion)}
                <% else %>
                  <p class="opacity-60">(pending / not yet run)</p>
                <% end %>
              </div>
            </div>
          </div>
        <% end %>

        <%= if @optimize_open do %>
          <div
            class="fixed inset-0 modal-overlay flex items-center justify-center z-50 p-4"
            phx-click="close_optimize"
          >
            <div
              class="modal-panel max-w-4xl w-full max-h-[90vh] overflow-auto"
              phx-click-away="close_optimize"
            >
              <div class="px-5 py-3 border-b app-hairline flex justify-between items-center">
                <h3 class="font-semibold">Optimize match prompt</h3>
                <button phx-click="close_optimize" class="btn btn-sm btn-ghost">Close</button>
              </div>

              <div class="p-4 space-y-4">
                <form phx-change="update_critique">
                  <label class="label">
                    <span class="label-text font-semibold">Free-form critique (optional)</span>
                  </label>
                  <textarea
                    name="critique"
                    class="textarea textarea-bordered w-full h-24"
                    placeholder="e.g. make bullets terser; require concrete identifiers; drop Auth & session when absent"
                  >{@critique}</textarea>
                </form>

                <div class="flex gap-2">
                  <button
                    phx-click="run_optimize"
                    disabled={@optimize_state == :running}
                    class="btn btn-primary"
                  >
                    <%= if @optimize_state == :running do %>
                      Calling claude…
                    <% else %>
                      Analyze + rewrite
                    <% end %>
                  </button>
                </div>

                <%= if @optimize_error do %>
                  <div class="alert alert-error text-sm">{@optimize_error}</div>
                <% end %>

                <%= if @optimize_result do %>
                  <div class="collapse collapse-open bg-base-200">
                    <div class="collapse-title font-semibold">Diagnosis</div>
                    <div class="collapse-content whitespace-pre-wrap text-sm">
                      {@optimize_result.diagnosis}
                    </div>
                  </div>
                  <div class="collapse collapse-open bg-base-200">
                    <div class="collapse-title font-semibold">Rationale</div>
                    <div class="collapse-content whitespace-pre-wrap text-sm">
                      {@optimize_result.rationale}
                    </div>
                  </div>
                  <details class="collapse collapse-arrow bg-base-200">
                    <summary class="collapse-title font-semibold">Improved prompt (preview)</summary>
                    <div class="collapse-content">
                      <pre class="whitespace-pre-wrap text-xs">{@optimize_result.improved}</pre>
                    </div>
                  </details>
                  <div class="flex justify-end">
                    <button
                      phx-click="apply_improved"
                      class="btn btn-success"
                      disabled={@optimize_state == :applied}
                    >
                      {if @optimize_state == :applied, do: "Applied ✓", else: "Apply to config"}
                    </button>
                  </div>
                <% end %>
              </div>
            </div>
          </div>
        <% end %>
      </div>
    </div>
    """
  end

  defp match_card_class(m) do
    base = "match-card"

    cond do
      m.is_bye -> base <> " is-bye"
      m.ready -> base <> " is-ready"
      true -> base
    end
  end

  defp input_label(nil), do: ""

  defp input_label("match:" <> _id), do: "(prev round)"

  defp input_label(path), do: Path.basename(path)

  # ── sidebar helpers ──────────────────────────────────────────────────

  defp list_entries do
    tournaments = Tournament.list_tournaments()
    configs = Config.list()

    tournament_names = MapSet.new(tournaments, & &1.name)

    dbs =
      Enum.map(tournaments, fn t ->
        config = Enum.find(configs, &(&1.name == t.name))
        status = if config, do: Runner.status(config), else: :done
        progress = Tournament.last_round_progress(t.path)

        Map.merge(t, %{
          status: status,
          has_db?: true,
          progress: progress,
          config_json: config && config.json
        })
      end)

    pending =
      configs
      |> Enum.reject(&MapSet.member?(tournament_names, &1.name))
      |> Enum.map(fn c ->
        %{
          name: c.name,
          path: nil,
          rounds: 0,
          matches: 0,
          final: nil,
          status: Runner.status(c),
          has_db?: false,
          progress: nil,
          config_json: c.json
        }
      end)

    (dbs ++ pending) |> Enum.sort_by(& &1.name)
  end

  defp load_bracket(%{has_db?: true, path: path}) when is_binary(path) do
    Tournament.bracket(path)
  end

  defp load_bracket(_), do: nil

  defp status_badge_class(:done), do: "badge badge-sm badge-success"
  defp status_badge_class(:running), do: "badge badge-sm badge-warning animate-pulse"
  defp status_badge_class(:failed), do: "badge badge-sm badge-error"
  defp status_badge_class(:pending), do: "badge badge-sm badge-ghost"
  defp status_badge_class(_), do: "badge badge-sm"

  defp status_label(:done), do: "done"
  defp status_label(:running), do: "running"
  defp status_label(:failed), do: "failed"
  defp status_label(:pending), do: "pending"
  defp status_label(_), do: "?"
end
