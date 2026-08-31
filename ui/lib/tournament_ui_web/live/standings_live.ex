defmodule TournamentUiWeb.StandingsLive do
  @moduledoc """
  /standings — the Swiss points table, rendered from the materialised read
  model.

  Deliberately not `/judge`. `docs/design/priority-tournament.md` splits the
  two surfaces: the judge is shown two items and nothing else, while
  standing is "a derived view for the queue". This page is that view — it is
  where the operator sees the ordering the comparisons produced, and it is
  what makes "work starts on the top group while the lower pairings are
  still being judged" possible before the last round is played.

  It carries its own nav key, `:standings`. Borrowing `:results` highlighted a
  different page than the one the operator was reading, and made the settled
  order look like a sub-view of the verdict log rather than the thing
  ninety-six comparisons were spent to produce.

  Every number here was computed by `bin/standings_view.py` through
  `bin/swiss.py`. This module renders and nothing else: no points, no ranks,
  no discard rule. Painting the page is one SELECT and never waits on a
  Python process — the recompute runs in an unlinked task on the poll tick,
  or synchronously when the operator asks for it by pressing Recompute.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.{Domains, Standings}
  alias TournamentUiWeb.DomainNav

  @impl true
  def mount(params, _session, socket) do
    if connected?(socket), do: :timer.send_interval(8_000, :refresh)

    {:ok,
     socket
     |> assign(domain_filter: DomainNav.normalize(params["domain"]))
     |> read_view()}
  end

  @impl true
  def handle_params(params, _uri, socket) do
    {:noreply,
     socket |> assign(domain_filter: DomainNav.normalize(params["domain"])) |> read_view()}
  end

  @impl true
  def handle_info(:refresh, socket) do
    parent = self()

    Task.start(fn ->
      Standings.materialise()
      send(parent, :view_recomputed)
    end)

    {:noreply, read_view(socket)}
  end

  def handle_info(:view_recomputed, socket), do: {:noreply, read_view(socket)}

  @impl true
  def handle_event("set_domain", %{"domain" => value}, socket) do
    {:noreply, push_patch(socket, to: standings_path(DomainNav.normalize(value)))}
  end

  def handle_event("recompute", _params, socket) do
    case Standings.materialise() do
      :ok ->
        {:noreply, socket |> read_view() |> put_flash(:info, "Points table recomputed.")}

      {:error, reason} ->
        {:noreply,
         socket
         |> read_view()
         |> put_flash(:error, "Could not recompute the points table: #{reason}")}
    end
  end

  defp read_view(socket) do
    assign(socket,
      table: Standings.table(domain: socket.assigns.domain_filter),
      domain_names: Domains.list() |> Enum.map(& &1.name) |> Enum.sort()
    )
  end

  defp standings_path(nil), do: "/standings"
  defp standings_path(domain), do: "/standings?" <> URI.encode_query(%{"domain" => domain})

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:standings}
      flash={@flash}
      max_width="max-w-6xl"
      title="Standings"
      subtitle={subtitle(@table)}
    >
      <:title_actions>
        <button phx-click="recompute" class="btn btn-ghost btn-sm" id="standings-recompute">
          Recompute
        </button>
        <.link navigate={DomainNav.judge_path(@domain_filter)} class="btn btn-primary btn-sm">
          Open Review queue →
        </.link>
      </:title_actions>

      <section class="app-card p-4 mb-5" aria-label="Standings scope">
        <div class="flex flex-wrap items-end gap-4">
          <label class="flex flex-col gap-1 min-w-64">
            <span class="text-[10px] uppercase tracking-wider opacity-55">Domain</span>
            <select
              name="domain"
              id="standings-domain-filter"
              phx-change="set_domain"
              class="select select-sm select-bordered"
            >
              <option value="">All domains</option>
              <%= for name <- @domain_names do %>
                <option value={name} selected={@domain_filter == name}>{name}</option>
              <% end %>
            </select>
          </label>

          <p id="standings-freshness" class="text-xs opacity-70 ml-auto text-right">
            {freshness(@table)}
          </p>
        </div>
      </section>

      <div
        :if={@table.unscored_verdicts != []}
        id="standings-unscored"
        class="app-card p-4 mb-5 border-l-4 border-warning"
      >
        <h2 class="font-semibold text-sm">Judgements outside the table</h2>
        <p class="text-xs opacity-70 mt-1">
          These verdicts are not scored by the engine — a retired vocabulary, or a
          rater saying they could not judge. They are counted here rather than folded
          in as nil-point matches, which would put a fully judged pool on zero.
        </p>
        <div class="mt-2 flex flex-wrap gap-2">
          <%= for row <- @table.unscored_verdicts do %>
            <span
              data-role="unscored-verdict"
              class="text-[11px] font-mono px-2 py-1 rounded bg-warning/15 text-warning"
            >
              {row.verdict} · {row.count}
            </span>
          <% end %>
        </div>
      </div>

      <%= if @table.tables == [] do %>
        <div class="app-card p-10 text-center" id="standings-empty">
          <h2 class="font-semibold">{empty_headline(@table)}</h2>
          <p class="text-sm opacity-60 mt-2">{empty_detail(@table)}</p>
        </div>
      <% end %>

      <%= for table <- @table.tables do %>
        <section
          class="app-card overflow-hidden mb-6"
          id={"standings-rubric-#{table.rubric}"}
          aria-label={"Points table — #{table.rubric}"}
        >
          <header class="px-4 py-3 border-b app-hairline flex flex-wrap items-baseline gap-2">
            <h2 class="font-semibold text-sm">{table.rubric}</h2>
            <span class="text-xs opacity-55 font-mono">v{table.rubric_version}</span>
            <span class="text-xs opacity-60 ml-auto" data-role="rubric-round">
              {round_statement(table)}
            </span>
          </header>

          <div class="overflow-x-auto">
            <table class="w-full text-sm" id={"standings-table-#{table.rubric}"}>
              <thead class="text-[10px] uppercase tracking-wider opacity-55">
                <tr class="border-b app-hairline">
                  <th class="text-left font-medium px-4 py-2 w-12">#</th>
                  <th class="text-left font-medium px-4 py-2">Item</th>
                  <th class="text-right font-medium px-4 py-2 w-20">Points</th>
                  <th class="text-right font-medium px-4 py-2 w-28">Matches played</th>
                  <th class="text-right font-medium px-4 py-2 w-28">W / D / L</th>
                </tr>
              </thead>
              <tbody class="divide-y app-hairline">
                <%= for entry <- table.standings do %>
                  <tr
                    id={"standing-#{entry.item_key}"}
                    data-points={entry.points}
                    data-top-group={to_string(entry.top_group)}
                    class={["align-top", entry.top_group && "bg-primary/5"]}
                  >
                    <td class="px-4 py-3 font-mono opacity-60">{rank_cell(entry)}</td>
                    <td class="px-4 py-3 min-w-0">
                      <div class="flex items-center gap-2 flex-wrap">
                        <span class="font-medium">{entry.title}</span>
                        <span
                          :if={entry.top_group}
                          data-role="top-group"
                          class="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-primary/15 text-primary font-semibold"
                        >
                          top group
                        </span>
                        <span
                          :if={entry.lost_honestly}
                          data-role="zero-points"
                          class="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-base-200 opacity-70"
                        >
                          zero points
                        </span>
                        <span
                          :if={entry.awaiting_first_result}
                          data-role="awaiting-first-result"
                          class="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-base-200 opacity-70"
                        >
                          no result yet
                        </span>
                      </div>
                      <div class="text-xs opacity-50 mt-1 font-mono break-all">
                        {entry.pool}<span :if={entry.source_ref}> · {entry.source_ref}</span>
                      </div>
                    </td>
                    <td class="px-4 py-3 text-right font-mono font-semibold">{entry.points}</td>
                    <td class="px-4 py-3 text-right font-mono">{entry.played}</td>
                    <td class="px-4 py-3 text-right font-mono opacity-70">
                      {entry.wins} / {entry.draws} / {entry.losses}
                    </td>
                  </tr>
                <% end %>
              </tbody>
            </table>
          </div>
          <p class="px-4 py-3 text-xs opacity-55 border-t app-hairline">
            A win is 3, a draw is 1, a loss is 0 — both win magnitudes score the same 3,
            because the magnitude is signal for the rubric optimizer, not a different
            number of points on the table. Zero points is a real position: an item that
            played and lost. "No result yet" is not — it is an item whose only pairing
            was discarded, so nothing was established about it.
          </p>

          <div class="border-t app-hairline" id={"discards-#{table.rubric}"}>
            <header class="px-4 py-3">
              <h3 class="font-semibold text-sm">Discarded — {length(table.discards)}</h3>
              <p class="text-xs opacity-55 mt-1">
                One named side left the pool for good; the item beside it stayed, with
                nothing recorded about it. A discard is not a score of zero.
              </p>
            </header>

            <p
              :if={table.discards == []}
              class="px-4 pb-4 text-sm opacity-60"
              id={"no-discards-#{table.rubric}"}
            >
              Nothing has been discarded in this view.
            </p>

            <ul :if={table.discards != []} class="divide-y app-hairline border-t app-hairline">
              <%= for item <- table.discards do %>
                <li
                  id={"discard-#{item.item_key}"}
                  class="px-4 py-3 flex items-start justify-between gap-4"
                >
                  <div class="min-w-0">
                    <div class="font-medium">{item.title}</div>
                    <div
                      :if={item.survivor_title}
                      class="text-xs opacity-60 mt-0.5"
                      data-role="survivor"
                    >
                      drawn against {item.survivor_title}, which stayed in the pool
                    </div>
                    <div class="text-xs opacity-50 mt-1 font-mono break-all">
                      {item.pool}<span :if={item.source_ref}> · {item.source_ref}</span>
                    </div>
                  </div>
                  <span
                    data-role="discard-verdict"
                    class="shrink-0 font-mono text-[11px] px-2 py-1 rounded bg-error/15 text-error"
                  >
                    {item.verdict}
                  </span>
                </li>
              <% end %>
            </ul>
          </div>
        </section>
      <% end %>
    </.workspace_page>
    """
  end

  defp subtitle(%{materialised?: false}),
    do: "The points table has not been computed yet — press Recompute."

  defp subtitle(%{totals: totals}) do
    "#{totals.items} items ordered by #{totals.matches} scored comparisons across " <>
      "#{totals.rubrics} rubric(s) · #{totals.discarded} discarded"
  end

  defp empty_headline(%{materialised?: false}), do: "The points table has not been computed"
  defp empty_headline(_), do: "No settled positions yet"

  defp empty_detail(%{materialised?: false}),
    do:
      "bin/standings_view.py has not run against this database. Press Recompute — " <>
        "an uncomputed table is not an empty corpus."

  defp empty_detail(_),
    do:
      "Position comes from pairwise comparison only. Judge some pairs and the " <>
        "table builds itself."

  defp freshness(%{materialised?: false}), do: "Never computed."

  defp freshness(%{computed_at: at, behind_by: 0}), do: "Computed #{at} UTC · current."

  defp freshness(%{computed_at: at, behind_by: behind}),
    do: "Computed #{at} UTC · #{behind} verdict(s) recorded since — press Recompute."

  defp round_statement(%{round: nil}), do: "No round has produced a verdict yet."

  defp round_statement(%{round: round, matches: matches}),
    do: "Round #{round} · #{matches} scored comparison(s)."

  defp rank_cell(%{rank: 0}), do: "—"
  defp rank_cell(%{rank: rank}), do: rank
end
