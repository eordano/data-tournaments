defmodule TournamentUiWeb.JudgementsLive do
  @moduledoc """
  /results — reviewer-facing, grouped comparison of human and model verdicts.

  Ratings are grouped by domain and match so a reviewer can see the source
  candidates, every rater, disagreement, confidence, and rationale together.
  The raw score tables remain available under the advanced Data inspector.

  ## Discards eject ONE side, and this page names which

  A discard verdict decides nothing about which of the two items is
  better, so `scoring_sides/2` keeps it (and `skip`) out of the win/loss
  aggregation `agreement/1` computes. `discard-a` ejects A and leaves B in
  the pool untouched, so `discard_entries/1` lists ONLY the named side and
  says who it was drawn against — listing both is how a malformed card used
  to destroy the good card beside it.

  ## Which rubrics this page reports

  Every pair rubric the fabric holds, from
  `TournamentUi.Judgement.pair_rubrics/0`. There is no rubric name in this
  file: a hand-kept list here is what made judgements written after a
  rubric moved invisible on this page while its export returned a different
  set again.

  ## Card bodies are markdown, and are rendered as markdown

  `TournamentUi.Judgement.decode_card/4` returns only `:title`, `:body` and
  `:source_ref` — never the `work_order` map the judging screen gates on
  before it reaches for `TournamentUiWeb.SafeMarkdown`. Copying that gate
  here would always take the plain-text branch, which is how every work
  order on this page came out reading `**Domain:** …` with its asterisks
  showing. `candidate_card/1` therefore renders through `SafeMarkdown`
  unconditionally: the bodies are generated markdown either way, and the
  sanitizer is what makes untrusted generator output safe to mark up at all.
  Stripping the asterisks instead would have hidden the same bug behind
  prettier text.

  ## Contrast lives in tokens, not in this file

  Muted text here carries `text-muted`, and semantic text carries
  `text-primary`/`text-info`/`text-success`/`text-warning`/`text-error`;
  `assets/css/app.css` maps all six to per-theme `--fg-*` values audited
  against the surfaces they land on. Muting with `opacity-*` instead is what
  put 36 items at 4.37:1, and it also multiplies into any coloured child, so
  a chip inside a dimmed row loses contrast twice over.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.{Domains, Judgement}
  alias TournamentUiWeb.DomainNav
  alias TournamentUiWeb.SafeMarkdown
  alias TournamentUiWeb.JudgeVerdictComponents, as: Verdicts

  @rater_filters [
    {"all", "All raters"},
    {"human", "Human only"},
    {"llm", "Models only"}
  ]

  @revise_needs_a_live_queue_row """
  Revising appends a new rating against the row the judge answered. A
  discard sweep cancels every pending row that shows a discarded item, so
  that row can be gone — and this control used to do nothing at all when it
  was, with no message.
  """

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket), do: :timer.send_interval(8_000, :refresh)

    {:ok,
     socket
     |> assign(
       rater_filter: "all",
       domain_filter: nil,
       rater_filters: @rater_filters,
       revising: nil
     )
     |> refresh()}
  end

  @impl true
  def handle_params(params, _uri, socket) do
    {:noreply,
     socket
     |> assign(domain_filter: DomainNav.normalize(params["domain"]))
     |> refresh()}
  end

  @impl true
  def handle_info(:refresh, socket), do: {:noreply, refresh(socket)}

  @impl true
  def handle_event("set_filter", %{"v" => value}, socket) do
    {:noreply, socket |> assign(rater_filter: value) |> refresh()}
  end

  def handle_event("set_domain", %{"domain" => value}, socket) do
    {:noreply, push_patch(socket, to: DomainNav.results_path(value))}
  end

  # ── Revision panel (wave-13 slice A; human ratings only) ──────────────

  def handle_event("open_revise", %{"pending-id" => pid, "rating-id" => rating_id}, socket) do
    pending_id = String.to_integer(pid)
    rating = Enum.find(socket.assigns.rows, &(&1.rating_id == rating_id))
    pending = Judgement.get_judgement(pending_id)

    case revise_blocker(rating, pending) do
      nil ->
        {:noreply,
         assign(socket,
           revising: %{
             pending_id: pending_id,
             previous_rating_id: rating_id,
             rubric: Verdicts.normalize_rubric(pending.output_definition || %{}),
             verdict_enum: (pending.output_definition || %{})["verdict_enum"] || [],
             chosen_verdict: rating.verdict,
             chosen_confidence: rating.confidence,
             rationale: rating.rationale || "",
             reason: "",
             error: nil
           }
         )}

      message ->
        {:noreply, socket |> assign(revising: nil) |> put_flash(:error, message) |> refresh()}
    end
  end

  def handle_event("cancel_revise", _params, socket) do
    {:noreply, assign(socket, revising: nil)}
  end

  # Same event contract the shared wheel/axis components emit.
  def handle_event("set_verdict", %{"v" => v}, socket) do
    {:noreply, update_revising(socket, &%{&1 | chosen_verdict: v, error: nil})}
  end

  def handle_event("set_revise_confidence", %{"v" => v}, socket) do
    {:noreply, update_revising(socket, &%{&1 | chosen_confidence: v})}
  end

  def handle_event("revise_form", params, socket) do
    {:noreply,
     update_revising(
       socket,
       &%{
         &1
         | reason: params["reason"] || &1.reason,
           rationale: params["rationale"] || &1.rationale
       }
     )}
  end

  def handle_event("submit_revise", params, socket) do
    case socket.assigns.revising do
      nil ->
        {:noreply, socket}

      rev ->
        reason = String.trim(params["reason"] || rev.reason)
        rationale = String.trim(params["rationale"] || rev.rationale)

        cond do
          rev.chosen_verdict in [nil, ""] ->
            {:noreply, update_revising(socket, &%{&1 | error: "pick a verdict"})}

          reason == "" ->
            {:noreply, update_revising(socket, &%{&1 | error: "a reason is required to revise"})}

          true ->
            case Judgement.revise_human(
                   rev.pending_id,
                   rev.previous_rating_id,
                   rev.chosen_verdict,
                   rev.chosen_confidence || "mid",
                   reason: reason,
                   rationale: if(rationale == "", do: nil, else: rationale),
                   revised_by: operator_identity()
                 ) do
              {:ok, _new_rating_id} ->
                {:noreply,
                 socket
                 |> assign(revising: nil)
                 |> put_flash(:info, "Judgement revised — the original stays in history.")
                 |> refresh()}

              {:error, reason_msg} ->
                {:noreply, update_revising(socket, &%{&1 | error: to_string(reason_msg)})}
            end
        end
    end
  end

  defp revise_blocker(nil, _pending), do: "that rating is no longer on this page — reload"

  defp revise_blocker(_rating, nil),
    do:
      "the queue row behind this judgement is gone, so it cannot be revised. " <>
        String.trim(@revise_needs_a_live_queue_row)

  defp revise_blocker(_rating, %{status: "done"}), do: nil

  defp revise_blocker(_rating, %{status: status}),
    do:
      "the queue row behind this judgement is '#{status}', not 'done', so it cannot be revised. " <>
        String.trim(@revise_needs_a_live_queue_row)

  defp update_revising(socket, fun) do
    case socket.assigns.revising do
      nil -> socket
      rev -> assign(socket, revising: fun.(rev))
    end
  end

  # Single-operator deployment: the revising principal is DT_OPERATOR
  # (same identity source the approval controls use); "anon" when unset —
  # mirrors submit_human's default rater identity.
  defp operator_identity, do: System.get_env("DT_OPERATOR") || "anon"

  defp refresh(socket) do
    rater_type =
      if socket.assigns.rater_filter == "all", do: nil, else: socket.assigns.rater_filter

    rows =
      Judgement.list_judgements(
        rater_type: rater_type,
        domain: socket.assigns.domain_filter,
        limit: 2_000
      )
      |> Enum.uniq_by(& &1.rating_id)
      |> Enum.sort_by(& &1.created_at, :desc)

    assign(socket,
      rows: rows,
      groups: group_rows(rows),
      discards: discard_entries(rows),
      counts: Judgement.counts(domain: socket.assigns.domain_filter),
      distribution: verdict_distribution(rows),
      total: length(rows),
      domain_names: Domains.list() |> Enum.map(& &1.name) |> Enum.sort()
    )
  end

  defp discard_entries(rows) do
    rows
    |> Enum.reject(&Map.get(&1, :superseded, false))
    |> Enum.flat_map(fn row ->
      case Judgement.discarded_side(row.verdict) do
        :a -> [discard_entry(row, row.card_a, row.card_b)]
        :b -> [discard_entry(row, row.card_b, row.card_a)]
        nil -> []
      end
    end)
    |> Enum.uniq_by(& &1.key)
    |> Enum.sort_by(& &1.created_at, :desc)
  end

  defp discard_entry(row, ejected, survivor) do
    %{
      key: Judgement.item_key(ejected),
      title: ejected.title,
      source_ref: ejected.source_ref,
      pool: row.domain_name || row.tournament_name,
      verdict: row.verdict,
      survivor_title: survivor.title,
      match_label: row.match_label,
      rater: row.rater,
      created_at: row.created_at
    }
  end

  defp group_rows(rows) do
    rows
    |> Enum.group_by(fn row -> {row.domain_name || row.tournament_name, row.match_id} end)
    |> Enum.map(fn {{name, match_id}, ratings} ->
      first = List.first(ratings)
      {superseded, effective} = Enum.split_with(ratings, &Map.get(&1, :superseded, false))

      %{
        key: "#{name}:#{match_id}",
        name: name,
        domain_name: first.domain_name,
        match_id: match_id,
        label: first.match_label,
        card_a: first.card_a,
        card_b: first.card_b,
        ratings: Enum.sort_by(effective, &rater_order/1),
        superseded_ratings: Enum.sort_by(superseded, & &1.created_at),
        agreement: agreement(effective),
        created_at: ratings |> Enum.map(& &1.created_at) |> Enum.max(fn -> "" end)
      }
    end)
    |> Enum.sort_by(& &1.created_at, :desc)
  end

  defp rater_order(row) do
    case Map.get(row.rater, "type") do
      "human" -> {0, ""}
      "llm" -> model_order(Map.get(row.rater, "model"))
      other -> {5, other || ""}
    end
  end

  defp model_order("moonshotai/kimi-k3"), do: {1, ""}
  defp model_order("z-ai/glm-5.2"), do: {2, ""}
  defp model_order("anthropic/claude-opus-5"), do: {3, ""}
  defp model_order(model), do: {4, model || ""}

  defp agreement(ratings) do
    human_sides = ratings |> scoring_sides("human") |> Enum.uniq()
    model_sides = scoring_sides(ratings, "llm")
    majority = majority_side(model_sides)

    human_discard =
      Enum.find(
        ratings,
        &(Map.get(&1.rater, "type") == "human" and Judgement.discard_verdict?(&1.verdict))
      )

    cond do
      human_discard ->
        %{
          label: "One side left the pool",
          tone: :neutral,
          detail:
            "#{humanize_verdict(human_discard.verdict)} — that side removed, not scored zero; " <>
              "the other stayed with nothing recorded"
        }

      human_sides == [] ->
        %{label: "Awaiting human baseline", tone: :neutral, detail: panel_detail(majority)}

      majority == nil ->
        %{label: "No model majority", tone: :neutral, detail: "Panel is split or absent"}

      majority in human_sides ->
        %{
          label: "Human and panel agree",
          tone: :agree,
          detail: "Both prefer #{side_label(majority)}"
        }

      true ->
        %{
          label: "Human and panel disagree",
          tone: :disagree,
          detail: "Panel prefers #{side_label(majority)}"
        }
    end
  end

  defp scoring_sides(ratings, rater_type) do
    ratings
    |> Enum.filter(&(Map.get(&1.rater, "type") == rater_type))
    |> Enum.reject(&(&1.verdict == "skip" or Judgement.discard_verdict?(&1.verdict)))
    |> Enum.map(&verdict_side(&1.verdict))
  end

  defp majority_side([]), do: nil

  defp majority_side(sides) do
    ranked = sides |> Enum.frequencies() |> Enum.sort_by(fn {_side, count} -> -count end)

    case ranked do
      [{side, _}] -> side
      [{_side, count}, {_other, count} | _] -> nil
      [{side, _} | _] -> side
      _ -> nil
    end
  end

  defp panel_detail(nil), do: "No model majority yet"
  defp panel_detail(side), do: "Panel prefers #{side_label(side)}"

  defp verdict_side("a-" <> _), do: "a"
  defp verdict_side("b-" <> _), do: "b"
  defp verdict_side("tie"), do: "tie"
  defp verdict_side(other), do: other

  defp side_label("a"), do: "A"
  defp side_label("b"), do: "B"
  defp side_label("tie"), do: "a tie"
  defp side_label(other), do: other

  defp verdict_distribution(rows) do
    rows
    |> Enum.group_by(& &1.verdict)
    |> Enum.map(fn {verdict, items} -> {verdict, length(items)} end)
    |> Enum.sort_by(fn {_verdict, count} -> -count end)
  end

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:results}
      flash={@flash}
      max_width="max-w-7xl"
      title="Results"
      subtitle={results_subtitle(@counts, @groups, @total)}
    >
      <:title_actions>
        <.link
          navigate={standings_path(@domain_filter)}
          class="btn btn-ghost btn-sm border app-hairline"
        >
          Standings →
        </.link>
        <.link
          :if={@counts.pending_human > 0}
          navigate={DomainNav.judge_path(@domain_filter)}
          class="btn btn-primary btn-sm"
        >
          Review {@counts.pending_human} pending →
        </.link>
      </:title_actions>

      <section class="app-card p-4 mb-5" aria-label="Result filters">
        <div class="flex flex-wrap items-end gap-4">
          <label class="flex flex-col gap-1 min-w-64">
            <span class="text-[10px] uppercase tracking-wider text-muted">Domain</span>
            <select name="domain" phx-change="set_domain" class="select select-sm select-bordered">
              <option value="">All domains</option>
              <%= for name <- @domain_names do %>
                <option value={name} selected={@domain_filter == name}>{name}</option>
              <% end %>
            </select>
          </label>

          <div>
            <div class="text-[10px] uppercase tracking-wider text-muted mb-1">Raters shown</div>
            <div class="flex gap-1">
              <%= for {value, label} <- @rater_filters do %>
                <button
                  phx-click="set_filter"
                  phx-value-v={value}
                  class={[
                    "btn btn-sm normal-case",
                    @rater_filter == value && "btn-primary",
                    @rater_filter != value && "btn-ghost border app-hairline"
                  ]}
                >
                  {label}
                </button>
              <% end %>
            </div>
          </div>

          <div class="ml-auto text-xs text-muted text-right">
            <div>{@total} ratings across {length(@groups)} matches</div>
            <.link
              href={export_path(@rater_filter, @domain_filter)}
              class="underline hover:text-base-content"
            >
              Export JSONL
            </.link>
          </div>
        </div>

        <div :if={@distribution != []} class="mt-4 pt-3 border-t app-hairline flex flex-wrap gap-2">
          <%= for {verdict, count} <- @distribution do %>
            <span class={["text-[11px] font-mono px-2 py-1 rounded", verdict_chip(verdict)]}>
              {humanize_verdict(verdict)} · {count}
            </span>
          <% end %>
        </div>
      </section>

      <section
        :if={@discards != []}
        class="app-card overflow-hidden mb-5"
        id="results-discards"
        aria-label="Discarded items"
      >
        <header class="px-5 py-3 border-b app-hairline">
          <h2 class="font-semibold text-sm">Discarded — {length(@discards)}</h2>
          <p class="text-xs text-muted mt-1">
            One named side of the pairing left the pool and is never paired again.
            The item it was drawn against stayed, with nothing recorded about it.
            Not counted in the win/loss aggregations above, and not a score of zero —
            these say the generation stage produced something it should not have.
          </p>
        </header>
        <ul class="divide-y app-hairline">
          <%= for item <- @discards do %>
            <li
              id={"discarded-#{discard_id(item)}"}
              class="px-5 py-3 flex items-start justify-between gap-4"
            >
              <div class="min-w-0">
                <div class="font-medium">{item.title}</div>
                <div :if={item.survivor_title} class="text-xs text-muted mt-0.5" data-role="survivor">
                  drawn against {item.survivor_title}, which stayed in the pool
                </div>
                <div class="text-xs text-muted mt-1 font-mono break-all">
                  {item.pool} · {item.match_label}<span :if={item.source_ref}>
                    · {item.source_ref}</span>
                </div>
              </div>
              <span
                data-role="discard-verdict"
                class={[
                  "shrink-0 font-mono text-[11px] px-2 py-1 rounded",
                  verdict_chip(item.verdict)
                ]}
              >
                {humanize_verdict(item.verdict)}
              </span>
            </li>
          <% end %>
        </ul>
      </section>

      <%= if @groups == [] do %>
        <div class="app-card p-10 text-center">
          <h2 class="font-semibold">No results for this view</h2>
          <p class="text-sm text-muted mt-2">
            Generate candidate pairs from a domain, then complete human or model reviews.
          </p>
          <div class="mt-4 flex justify-center gap-2">
            <.link navigate="/domains" class="btn btn-primary btn-sm">Open Domains</.link>
            <.link navigate={DomainNav.judge_path(@domain_filter)} class="btn btn-ghost btn-sm">
              Open Review queue
            </.link>
          </div>
        </div>
      <% else %>
        <div class="space-y-5" id="result-groups">
          <%= for group <- @groups do %>
            <article class="app-card overflow-hidden" id={"result-#{group.key}"}>
              <header class="px-5 py-4 border-b app-hairline flex items-start justify-between gap-4">
                <div class="min-w-0">
                  <div class="flex items-center gap-2 flex-wrap">
                    <h2 class="font-semibold truncate">{group.name}</h2>
                    <span class="text-xs font-mono text-muted">{group.label}</span>
                  </div>
                  <div class="text-xs text-muted mt-1">
                    Match #{group.match_id} · {length(group.ratings)} ratings
                  </div>
                </div>
                <div class="flex items-center gap-2 shrink-0">
                  <.agreement_badge agreement={group.agreement} />
                  <.link
                    :if={group.domain_name}
                    navigate={"/domains/#{group.domain_name}/edit"}
                    class="btn btn-xs btn-ghost"
                  >
                    Configure domain
                  </.link>
                </div>
              </header>

              <div class="grid lg:grid-cols-2 gap-px bg-base-content/10">
                <.candidate_card side="A" card={group.card_a} />
                <.candidate_card side="B" card={group.card_b} />
              </div>

              <div class="px-5 py-4">
                <div class="text-[10px] uppercase tracking-wider text-muted mb-2">
                  Rater comparison
                </div>
                <div class="divide-y app-hairline">
                  <%= for rating <- group.ratings do %>
                    <div
                      class="grid gap-2 py-3 md:grid-cols-[minmax(12rem,1fr)_10rem_5rem_minmax(16rem,2fr)_auto] items-start text-xs"
                      id={"rating-#{rating.rating_id}"}
                    >
                      <div class="min-w-0">
                        <.rater_pill rater={rating.rater} />
                        <div :if={rating.revised} class="mt-1 flex flex-wrap items-center gap-1">
                          <span
                            class="px-1.5 py-0.5 rounded bg-warning/15 text-warning text-[10px] font-semibold uppercase tracking-wide"
                            title={rating.revision_reason}
                            data-role="revised-chip"
                          >
                            revised
                          </span>
                          <span class="text-muted text-[10px]" data-role="revision-reason">
                            {rating.revision_reason}
                          </span>
                        </div>
                        <div
                          :if={rating.revised && used_downstream?(rating)}
                          class="mt-0.5 text-[10px] text-muted italic"
                          data-role="revised-after-use"
                        >
                          revised after use — downstream outcomes unaffected
                        </div>
                      </div>
                      <span class={["font-mono px-2 py-1 rounded w-fit", verdict_chip(rating.verdict)]}>
                        {humanize_verdict(rating.verdict)}
                      </span>
                      <span class={["px-2 py-1 rounded w-fit", conf_chip(rating.confidence)]}>
                        {rating.confidence}
                      </span>
                      <p class="opacity-70 leading-5">
                        {rating.rationale || "No rationale recorded."}
                      </p>
                      <div class="shrink-0">
                        <button
                          :if={human_rating?(rating) && revisable?(rating)}
                          id={"revise-#{rating.rating_id}"}
                          phx-click="open_revise"
                          phx-value-pending-id={rating.pending_id}
                          phx-value-rating-id={rating.rating_id}
                          class="btn btn-xs btn-ghost border app-hairline"
                        >
                          Revise
                        </button>
                        <span
                          :if={human_rating?(rating) && !revisable?(rating)}
                          id={"revise-unavailable-#{rating.rating_id}"}
                          data-role="revise-unavailable"
                          class="text-[10px] text-muted italic block max-w-48 leading-4"
                        >
                          {unrevisable_reason(rating)}
                        </span>
                      </div>
                    </div>
                    <.revision_panel
                      :if={@revising && @revising.previous_rating_id == rating.rating_id}
                      revising={@revising}
                    />
                  <% end %>
                </div>

                <details
                  :if={group.superseded_ratings != []}
                  class="mt-3 rounded-lg border app-hairline"
                  id={"history-#{group.key}"}
                >
                  <summary class="px-3 py-2 cursor-pointer text-xs opacity-70 hover:opacity-100">
                    history ({length(group.superseded_ratings)})
                  </summary>
                  <div class="px-3 pb-3 divide-y app-hairline">
                    <%= for rating <- group.superseded_ratings do %>
                      <div
                        class="grid gap-2 py-3 md:grid-cols-[minmax(12rem,1fr)_10rem_5rem_minmax(16rem,2fr)] items-start text-xs text-muted line-through"
                        id={"superseded-#{rating.rating_id}"}
                        data-superseded="true"
                      >
                        <.rater_pill rater={rating.rater} />
                        <span class={[
                          "font-mono px-2 py-1 rounded w-fit",
                          verdict_chip(rating.verdict)
                        ]}>
                          {humanize_verdict(rating.verdict)}
                        </span>
                        <span class={["px-2 py-1 rounded w-fit", conf_chip(rating.confidence)]}>
                          {rating.confidence}
                        </span>
                        <p class="leading-5">
                          {rating.rationale || "No rationale recorded."}
                        </p>
                      </div>
                    <% end %>
                  </div>
                </details>
              </div>
            </article>
          <% end %>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  # Human-only feature this wave: LLM ratings carry no revise control.
  defp human_rating?(rating) do
    Map.get(rating.rater, "type") == "human" and is_integer(rating.pending_id)
  end

  defp revisable?(rating), do: Map.get(rating, :pending_status) == "done"

  defp unrevisable_reason(%{pending_status: nil}),
    do: "not revisable — its queue row is gone (a discard sweep cancels rows)"

  defp unrevisable_reason(%{pending_status: status}),
    do: "not revisable — its queue row is #{status} (a discard sweep cancels rows)"

  # Honest caption trigger: the pair already fed downstream outcomes when
  # the source match recorded a winner (bracket advancement). We never
  # rewrite those outcomes — we only say so.
  defp used_downstream?(rating) do
    payload = rating.trace_payload || %{}
    Map.get(payload, "winner_id") != nil
  end

  attr :revising, :map, required: true

  defp revision_panel(assigns) do
    ~H"""
    <div id="revision-panel" class="my-3 rounded-lg border border-warning/40 bg-warning/5 p-4">
      <div class="flex items-center justify-between gap-3 mb-3">
        <div class="text-xs font-semibold uppercase tracking-wider opacity-70">
          Revise judgement — appends a new rating; the original stays in history
        </div>
        <button phx-click="cancel_revise" class="btn btn-ghost btn-xs" id="cancel-revise">
          Cancel
        </button>
      </div>

      <%= cond do %>
        <% @revising.rubric.wheel && @revising.rubric.kind == "single" -> %>
          <Verdicts.verdict_axis
            wheel={@revising.rubric.wheel}
            chosen={@revising.chosen_verdict}
            subject={List.first(@revising.rubric.subjects)}
          />
          <Verdicts.operational_verdicts
            verdicts={@revising.rubric.operational}
            chosen={@revising.chosen_verdict}
          />
        <% @revising.rubric.wheel -> %>
          <Verdicts.verdict_wheel
            wheel={@revising.rubric.wheel}
            chosen={@revising.chosen_verdict}
            subject={List.first(@revising.rubric.subjects)}
          />
          <Verdicts.operational_verdicts
            verdicts={@revising.rubric.operational}
            chosen={@revising.chosen_verdict}
          />
        <% true -> %>
          <div class="grid grid-cols-2 gap-2 max-w-xl">
            <%= for v <- @revising.verdict_enum do %>
              <button
                type="button"
                id={"revise-verdict-#{v}"}
                phx-click="set_verdict"
                phx-value-v={v}
                class={[
                  "btn btn-sm normal-case justify-start",
                  @revising.chosen_verdict == v && "btn-primary",
                  @revising.chosen_verdict != v && "btn-ghost border app-hairline"
                ]}
              >
                {humanize_verdict(v)}
              </button>
            <% end %>
          </div>
      <% end %>

      <div class="mt-3 flex items-center gap-2">
        <span class="text-[10px] uppercase tracking-wider text-muted">Confidence</span>
        <%= for c <- ~w(low mid high) do %>
          <button
            type="button"
            id={"revise-confidence-#{c}"}
            phx-click="set_revise_confidence"
            phx-value-v={c}
            class={[
              "btn btn-xs normal-case",
              @revising.chosen_confidence == c && "btn-primary",
              @revising.chosen_confidence != c && "btn-ghost border app-hairline"
            ]}
          >
            {c}
          </button>
        <% end %>
      </div>

      <form
        phx-change="revise_form"
        phx-submit="submit_revise"
        id="revise-form"
        class="mt-3 space-y-3"
      >
        <label class="block">
          <span class="text-[10px] uppercase tracking-wider text-muted">
            Reason for revision (required)
          </span>
          <textarea
            name="reason"
            id="revise-reason"
            rows="2"
            required
            placeholder="Why is the original judgement being revised?"
            class="textarea textarea-bordered textarea-sm w-full mt-1"
          >{@revising.reason}</textarea>
        </label>
        <label class="block">
          <span class="text-[10px] uppercase tracking-wider text-muted">Rationale (optional)</span>
          <textarea
            name="rationale"
            id="revise-rationale"
            rows="2"
            class="textarea textarea-bordered textarea-sm w-full mt-1"
          >{@revising.rationale}</textarea>
        </label>
        <div :if={@revising.error} class="text-xs text-error" id="revise-error">
          {@revising.error}
        </div>
        <button type="submit" class="btn btn-primary btn-sm" id="submit-revise">
          Submit revision
        </button>
      </form>
    </div>
    """
  end

  attr :agreement, :map, required: true

  defp agreement_badge(assigns) do
    ~H"""
    <div class={[
      "rounded-lg px-3 py-1.5 text-xs",
      @agreement.tone == :agree && "bg-success/15 text-success",
      @agreement.tone == :disagree && "bg-warning/15 text-warning",
      @agreement.tone == :neutral && "bg-base-200 text-base-content/75"
    ]}>
      <div class="font-medium">{@agreement.label}</div>
      <div class="text-[10px] mt-0.5">{@agreement.detail}</div>
    </div>
    """
  end

  attr :side, :string, required: true
  attr :card, :map, required: true

  defp candidate_card(assigns) do
    ~H"""
    <section class="bg-base-100 p-5 min-w-0">
      <div class="flex items-start gap-3">
        <div class="size-7 shrink-0 rounded-full bg-primary/15 text-primary font-bold text-xs grid place-items-center">
          {@side}
        </div>
        <div class="min-w-0">
          <h3 class="font-medium">{@card.title}</h3>
          <div
            :if={@card.body not in [nil, ""]}
            class="prose prose-sm max-w-none mt-2 max-h-64 overflow-y-auto pr-1"
            data-role="card-body"
          >
            {SafeMarkdown.render(@card.body)}
          </div>
          <div
            :if={@card.source_ref}
            class="font-mono text-[11px] text-muted mt-3 break-all"
            title={@card.source_ref}
          >
            {@card.source_ref}
          </div>
        </div>
      </div>
    </section>
    """
  end

  attr :rater, :map, required: true

  defp rater_pill(assigns) do
    ~H"""
    <%= case Map.get(@rater, "type") do %>
      <% "human" -> %>
        <span class="font-medium text-success">
          Human · {Map.get(@rater, "userId") || "anonymous"}
        </span>
      <% "llm" -> %>
        <span class="font-mono text-info break-all">
          {short_model(Map.get(@rater, "model") || "Unknown model")}
        </span>
      <% other -> %>
        <span class="font-mono text-muted">{other || "Unknown rater"}</span>
    <% end %>
    """
  end

  defp short_model("moonshotai/kimi-k3"), do: "Kimi K3"
  defp short_model("z-ai/glm-5.2"), do: "GLM 5.2"
  defp short_model("anthropic/claude-opus-5"), do: "Claude Opus 5"
  defp short_model("openai/" <> model), do: model
  defp short_model(model), do: model

  defp results_subtitle(%{db_present: false}, _groups, _total),
    do: "The judgement database has not been initialized yet."

  defp results_subtitle(counts, groups, total) do
    "Compare #{total} ratings across #{length(groups)} matches · #{counts.pending_human} human and #{counts.pending_llm} model reviews pending"
  end

  defp export_path(rater_filter, domain) do
    params =
      %{}
      |> maybe_put_param("rater_type", if(rater_filter == "all", do: nil, else: rater_filter))
      |> maybe_put_param("domain", domain)

    case URI.encode_query(params) do
      "" -> "/api/judgements/export"
      query -> "/api/judgements/export?" <> query
    end
  end

  defp standings_path(nil), do: "/standings"
  defp standings_path(domain), do: "/standings?domain=" <> URI.encode_www_form(domain)

  defp maybe_put_param(params, _key, nil), do: params
  defp maybe_put_param(params, key, value), do: Map.put(params, key, value)

  defp humanize_verdict("discard-a"), do: "Discard A"
  defp humanize_verdict("discard-b"), do: "Discard B"

  defp humanize_verdict(value) when is_binary(value) do
    value
    |> String.replace("-", " ")
    |> String.replace_prefix("a ", "A · ")
    |> String.replace_prefix("b ", "B · ")
    |> String.capitalize()
  end

  defp discard_id(item) do
    item.key
    |> String.replace(~r/[^A-Za-z0-9]+/, "-")
    |> String.trim("-")
    |> String.downcase()
  end

  defp verdict_chip(value) do
    cond do
      Judgement.discard_verdict?(value) -> "bg-error/15 text-error"
      value == "skip" -> "bg-base-200 text-muted"
      value == "tie" -> "bg-warning/15 text-warning"
      String.starts_with?(value, "a-") -> "bg-info/15 text-info"
      String.starts_with?(value, "b-") -> "bg-success/15 text-success"
      true -> "bg-base-200"
    end
  end

  defp conf_chip("high"), do: "bg-success/10 text-success"
  defp conf_chip("low"), do: "bg-warning/10 text-warning"
  defp conf_chip(_), do: "bg-base-200 text-muted"
end
