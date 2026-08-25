defmodule TournamentUiWeb.JudgeLive do
  @moduledoc """
  /judge — present pending judgements to a human rater and submit verdicts.

  v0 UI:
    * Sidebar lists pending rows, newest at the bottom (FIFO).
    * Main pane shows the active row's trace payload (the agent's
      synthesis, both inputs, the chosen winner) plus a verdict picker,
      a 3-state confidence selector, an optional rationale textarea.
    * Submit posts via TournamentUi.Judgement.submit_human/4 and
      advances to the next pending row.
  """
  use TournamentUiWeb, :live_view
  alias TournamentUi.{Domains, Judgement, LangfusePrompts}
  alias TournamentUiWeb.DomainNav
  alias TournamentUiWeb.JudgeVerdictComponents, as: Verdicts
  alias TournamentUiWeb.SafeMarkdown

  @impl true
  def mount(params, _session, socket) do
    if connected?(socket), do: :timer.send_interval(5_000, :refresh)

    # Auto-initialize the judgement-fabric DB on first visit (F-4).
    # Attempted at most once per mount; failure degrades to the empty
    # state plus a warning banner — never crashes, never loops.
    init_warning =
      case Judgement.ensure_initialized() do
        :ok -> nil
        {:error, reason} -> reason
      end

    {:ok,
     socket
     |> assign(
       domain_filter: DomainNav.normalize(params["domain"]),
       init_warning: init_warning,
       expanded_candidate: nil
     )
     |> refresh()
     |> assign(submit_state: :idle, submit_error: nil)}
  end

  @impl true
  def handle_params(params, _uri, socket) do
    {:noreply,
     socket |> assign(domain_filter: DomainNav.normalize(params["domain"])) |> refresh()}
  end

  @impl true
  def handle_info(:refresh, socket), do: {:noreply, refresh(socket)}

  def handle_info({:judge_brief_loaded, pending_id, text}, socket) do
    if socket.assigns.active && socket.assigns.active.id == pending_id do
      brief = if is_binary(text) and text != "", do: text, else: socket.assigns.judge_brief
      {:noreply, assign(socket, judge_brief: brief, judge_brief_loading: false)}
    else
      {:noreply, socket}
    end
  end

  @impl true
  def handle_event("select", %{"id" => id}, socket) do
    pending_id = String.to_integer(id)
    {:noreply, select_pending(socket, pending_id)}
  end

  def handle_event("filter_domain", %{"domain" => domain}, socket) do
    {:noreply, push_patch(socket, to: DomainNav.judge_path(domain))}
  end

  def handle_event("expand_candidate", %{"side" => side}, socket) do
    # Toggle: clicking the same side (or "back") collapses to side-by-side.
    current = socket.assigns.expanded_candidate
    next = if current == side, do: nil, else: side
    {:noreply, assign(socket, expanded_candidate: next)}
  end

  def handle_event("keydown", %{"key" => key}, socket) do
    cond do
      socket.assigns.active == nil ->
        {:noreply, socket}

      # Number keys → verdict. Wheel templates use NUMPAD GEOMETRY
      # (7/8/9 = nw/n/ne, 4/6 = w/e, 1/2/3 = sw/s/se — top-row digits
      # accepted too); non-wheel templates keep the legacy behavior of
      # indexing into verdict_enum.
      key in ~w(1 2 3 4 5 6 7 8 9) ->
        case wheel_key_verdict(active_rubric(socket), key) do
          {:wheel, nil} ->
            {:noreply, socket}

          {:wheel, v} ->
            {:noreply, assign(socket, chosen_verdict: v, submit_error: nil)}

          :flat ->
            idx = String.to_integer(key) - 1
            verdicts = socket.assigns.active.output_definition["verdict_enum"] || []

            case Enum.at(verdicts, idx) do
              nil -> {:noreply, socket}
              v -> {:noreply, assign(socket, chosen_verdict: v, submit_error: nil)}
            end
        end

      # Space or Enter → submit if a verdict is picked (multi-subject
      # rubrics advance to the next subject until the last one).
      key in [" ", "Enter"] and socket.assigns.chosen_verdict != nil ->
        rubric = active_rubric(socket)

        cond do
          length(rubric.subjects) <= 1 ->
            handle_event("submit", %{}, socket)

          socket.assigns.subject_index < length(rubric.subjects) - 1 ->
            handle_event("next_subject", %{}, socket)

          true ->
            handle_event("submit_judgement", %{}, socket)
        end

      # S → skip
      key in ["s", "S"] ->
        handle_event("skip", %{}, socket)

      # J/K or ArrowDown/Up → next/prev pending row
      key in ["j", "J", "ArrowDown"] ->
        {:noreply, move_active(socket, +1)}

      key in ["k", "K", "ArrowUp"] ->
        {:noreply, move_active(socket, -1)}

      true ->
        {:noreply, socket}
    end
  end

  def handle_event("change", params, socket) do
    {:noreply,
     assign(socket,
       chosen_verdict: params["verdict"] || socket.assigns.chosen_verdict,
       chosen_confidence: params["confidence"] || socket.assigns.chosen_confidence,
       rationale: params["rationale"] || socket.assigns.rationale
     )}
  end

  @impl true
  def handle_event("set_verdict", %{"v" => v}, socket) do
    {:noreply, assign(socket, chosen_verdict: v, submit_error: nil)}
  end

  @impl true
  def handle_event("set_confidence", %{"v" => v}, socket) do
    {:noreply, assign(socket, chosen_confidence: v)}
  end

  @impl true
  def handle_event("submit", _params, socket) do
    %{active: active, chosen_verdict: v, chosen_confidence: c, rationale: r} =
      socket.assigns

    cond do
      active == nil ->
        {:noreply, assign(socket, submit_error: "no active pending row")}

      v == nil or v == "" ->
        {:noreply, assign(socket, submit_error: "pick a verdict before submitting")}

      true ->
        case Judgement.submit_human(active.id, v, c, rationale: r) do
          {:ok, _rating_id} ->
            {:noreply,
             socket
             |> put_flash(:info, "Judgement recorded.")
             |> refresh()
             |> reset_form()}

          {:error, reason} ->
            {:noreply, assign(socket, submit_error: to_string(reason), submit_state: :idle)}
        end
    end
  end

  @impl true
  def handle_event("next_subject", _params, socket) do
    %{active: active, chosen_verdict: v, chosen_confidence: c, rationale: r} = socket.assigns

    cond do
      active == nil ->
        {:noreply, socket}

      v == nil or v == "" ->
        {:noreply, assign(socket, submit_error: "pick a verdict for this subject first")}

      true ->
        rubric = active_rubric(socket)
        idx = socket.assigns.subject_index

        if idx >= length(rubric.subjects) - 1 do
          {:noreply, socket}
        else
          subject = Enum.at(rubric.subjects, idx)

          answers =
            Map.put(socket.assigns.subject_answers, subject, %{
              "verdict" => v,
              "confidence" => c,
              "rationale" => r
            })

          {:noreply,
           assign(socket,
             subject_answers: answers,
             subject_index: idx + 1,
             chosen_verdict: nil,
             chosen_confidence: "mid",
             rationale: "",
             submit_error: nil
           )}
        end
    end
  end

  # Multi-subject submit path (wave-12). Params carry the full per-subject
  # payload (%{"subjects" => %{"idea" => %{"verdict" => ...}, ...}}) via
  # hidden form inputs; the assigns-derived fallback keeps the keyboard
  # path working. The single-subject "submit" path above is untouched.
  @impl true
  def handle_event("submit_judgement", params, socket) do
    case socket.assigns.active do
      nil ->
        {:noreply, assign(socket, submit_error: "no active pending row")}

      active ->
        rubric = active_rubric(socket)
        subjects = subjects_from_params(params, socket, rubric)

        missing =
          Enum.filter(rubric.subjects, fn s ->
            v = get_in(subjects, [s, "verdict"])
            v == nil or v == ""
          end)

        if missing != [] do
          {:noreply,
           assign(socket,
             submit_error: "missing verdict for subject: #{Enum.join(missing, ", ")}"
           )}
        else
          case Judgement.submit_human_subjects(active.id, subjects,
                 subject_order: rubric.subjects
               ) do
            {:ok, _rating_id} ->
              {:noreply,
               socket
               |> put_flash(:info, "Judgement recorded.")
               |> refresh()
               |> reset_form()}

            {:error, reason} ->
              {:noreply, assign(socket, submit_error: to_string(reason), submit_state: :idle)}
          end
        end
    end
  end

  @impl true
  def handle_event("skip", _params, socket) do
    case socket.assigns.active do
      nil ->
        {:noreply, socket}

      active ->
        # "skip" verdict is reserved on every rubric; submit it with mid
        # confidence and no rationale. (Rubric versioning ensures `skip`
        # remains a valid verdict.)
        case Judgement.submit_human(active.id, "skip", "mid", rationale: "(skipped)") do
          {:ok, _} ->
            {:noreply, refresh(socket) |> reset_form() |> put_flash(:info, "Skipped.")}

          {:error, reason} ->
            {:noreply, assign(socket, submit_error: to_string(reason))}
        end
    end
  end

  defp move_active(socket, delta) do
    case socket.assigns.active do
      nil ->
        socket

      active ->
        pending = socket.assigns.pending
        idx = Enum.find_index(pending, &(&1.id == active.id))

        case idx do
          nil ->
            socket

          i ->
            next_i = rem(i + delta + length(pending), length(pending))

            case Enum.at(pending, next_i) do
              nil -> socket
              row -> activate(socket, row)
            end
        end
    end
  end

  defp refresh(socket) do
    pending =
      Judgement.list_pending(
        rater_type: "human",
        domain: socket.assigns.domain_filter,
        limit: 500
      )

    counts = Judgement.counts(domain: socket.assigns.domain_filter)

    socket
    |> assign(
      pending: pending,
      counts: counts,
      db_present: counts.db_present,
      domains: Domains.list()
    )
    |> select_active(pending)
  end

  defp select_active(socket, []),
    do:
      assign(socket, active: nil, judge_brief: nil, judge_brief_loading: false)
      |> reset_form()

  defp select_active(socket, [head | _]) do
    case socket.assigns[:active] do
      nil ->
        activate(socket, head)

      %{id: id} when id == head.id ->
        assign(socket, active: head)

      # Active row no longer pending (done elsewhere) → advance to head.
      _ ->
        if Enum.any?(socket.assigns.pending, &(&1.id == socket.assigns.active.id)) do
          socket
        else
          activate(socket, head)
        end
    end
  end

  defp select_pending(socket, pending_id) do
    case Enum.find(socket.assigns.pending, &(&1.id == pending_id)) do
      nil -> socket
      row -> activate(socket, row)
    end
  end

  defp activate(socket, row) do
    socket =
      assign(socket,
        active: row,
        judge_brief: fallback_judge_brief(row),
        judge_brief_loading: false,
        expanded_candidate: nil
      )
      |> reset_form()

    maybe_load_judge_brief(socket, row)
  end

  defp maybe_load_judge_brief(socket, %{judge_prompt_name: name, id: pending_id})
       when is_binary(name) do
    if connected?(socket) do
      parent = self()

      Task.start(fn ->
        text =
          case LangfusePrompts.get(name, "production") do
            value when is_binary(value) and value != "" -> value
            _ -> nil
          end

        send(parent, {:judge_brief_loaded, pending_id, text})
      end)

      assign(socket, judge_brief_loading: true)
    else
      socket
    end
  end

  defp maybe_load_judge_brief(socket, _row), do: socket

  defp fallback_judge_brief(row) do
    category = row[:domain_description] || "Choose the candidate more worth surfacing."

    "#{category}\n\nPrefer specific, evidence-backed, actionable candidates. Use confidence to show how certain you are; skip when the pair cannot be judged fairly."
  end

  defp reset_form(socket) do
    assign(socket,
      chosen_verdict: nil,
      chosen_confidence: "mid",
      rationale: "",
      submit_error: nil,
      subject_index: 0,
      subject_answers: %{}
    )
  end

  # Normalized v2 rubric (kind/subjects/wheel) for the active row — every
  # new output_definition key is optional; legacy templates normalize to
  # kind "pair", subjects ["execution"], wheel nil.
  defp active_rubric(socket) do
    Verdicts.normalize_rubric((socket.assigns.active || %{})[:output_definition] || %{})
  end

  # Numpad-geometry key handling. :flat → legacy index-into-enum behavior.
  defp wheel_key_verdict(%{wheel: nil}, _key), do: :flat

  defp wheel_key_verdict(%{wheel: wheel, kind: kind}, key) do
    pos = Verdicts.numpad_position(key)

    cond do
      pos == nil -> {:wheel, nil}
      kind == "single" and pos not in Verdicts.axis_positions() -> {:wheel, nil}
      true -> {:wheel, Map.get(wheel, pos)}
    end
  end

  defp subjects_from_params(%{"subjects" => subjects}, _socket, _rubric) when is_map(subjects) do
    subjects
    |> Enum.filter(fn {_subject, entry} -> is_map(entry) end)
    |> Map.new()
  end

  defp subjects_from_params(_params, socket, rubric) do
    # Keyboard-driven submit (no form params): previously captured
    # subjects live in assigns; the active subject comes from live state.
    idx = min(socket.assigns.subject_index, length(rubric.subjects) - 1)
    active_subject = Enum.at(rubric.subjects, idx)

    Map.put(socket.assigns.subject_answers, active_subject, %{
      "verdict" => socket.assigns.chosen_verdict,
      "confidence" => socket.assigns.chosen_confidence,
      "rationale" => socket.assigns.rationale
    })
  end

  # ─────────────────────────────────────────────────────────────────────
  # Render
  # ─────────────────────────────────────────────────────────────────────

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_split current={:judge} id="judge-shell" phx-hook="JudgeShortcuts">
      <%!-- Contract §7: no sidebar during judging. The queue picker, domain
            selector, counts, and Results link the old w-80 aside carried
            move into this full-width header bar; the queue itself becomes a
            horizontal strip. Judging content gets the whole viewport. --%>
      <main class="flex-1 flex flex-col overflow-hidden workspace">
        <div class="px-6 pt-3 pb-2 bg-base-100 border-b app-hairline" id="judge-queue-bar">
          <div class="flex items-center gap-3 flex-wrap">
            <h2 class="font-semibold text-base leading-tight">Review queue</h2>
            <form phx-change="filter_domain">
              <select
                name="domain"
                id="judge-domain-filter"
                class="select select-xs select-bordered w-56 font-mono"
              >
                <option value="" selected={is_nil(@domain_filter)}>All domains</option>
                <%= for d <- @domains do %>
                  <option value={d.name} selected={d.name == @domain_filter}>
                    {d.name} ({d.pending_human} pending)
                  </option>
                <% end %>
              </select>
            </form>
            <p class="text-xs opacity-60">
              <%= if @db_present do %>
                {length(@pending)} pending<%= if @domain_filter do %>
                  in {@domain_filter}
                <% end %>
                · {@counts.judgements_total} ratings recorded
              <% else %>
                fabric DB missing — run <code class="font-mono">bin/judgement.py init</code>
              <% end %>
            </p>
            <.link
              navigate={DomainNav.results_path(@domain_filter)}
              class="text-xs opacity-60 hover:opacity-100 ml-auto shrink-0"
            >
              compare results →
            </.link>
          </div>

          <div
            :if={@init_warning}
            id="judge-init-warning"
            class="mt-2 px-3 py-2 rounded text-xs bg-warning/10 flex items-start gap-1.5"
          >
            <.icon name="hero-exclamation-triangle" class="size-3.5 shrink-0 mt-0.5 text-warning" />
            <span>
              Couldn't auto-initialize the judgement DB — run
              <code class="font-mono">python3 bin/judgement.py init</code>
              from the project root.
            </span>
          </div>

          <div
            :if={@domain_filter}
            class="mt-2 px-3 py-1.5 rounded text-xs flex items-center justify-between gap-2 bg-primary/5"
          >
            <span class="truncate">
              Filtered to <strong class="font-mono">{@domain_filter}</strong>
            </span>
            <.link patch="/judge" class="opacity-60 hover:opacity-100">clear</.link>
          </div>

          <div class="flex gap-1.5 overflow-x-auto mt-2 pb-1" id="judge-queue-strip">
            <%= for row <- @pending do %>
              <button
                phx-click="select"
                phx-value-id={row.id}
                class={[
                  "entry-row shrink-0 w-64 text-left px-3 py-2 cursor-pointer",
                  @active && @active.id == row.id && "is-selected"
                ]}
              >
                <div class="flex items-center gap-2">
                  <div class="font-medium text-sm truncate flex-1">
                    {row_headline(row)}
                  </div>
                  <span class="text-[10px] font-mono opacity-50">#{row.id}</span>
                </div>
                <div class="text-xs opacity-60 mt-1 truncate">
                  {Map.get(row.trace_payload, "label") || "match #{row.match_id}"} · {row.domain_name ||
                    Path.basename(row.tournament_db_path, ".db")}
                </div>
              </button>
            <% end %>
            <%= if @pending == [] do %>
              <div class="px-4 py-2 rounded-lg border border-dashed app-hairline text-center shrink-0">
                <span class="text-sm font-medium opacity-80">Inbox zero</span>
                <span class="text-xs opacity-60 ml-2">No pending human judgements right now.</span>
              </div>
            <% end %>
          </div>
        </div>

        <%= if @active do %>
          <.judge_pane
            active={@active}
            judge_brief={@judge_brief}
            judge_brief_loading={@judge_brief_loading}
            chosen_verdict={@chosen_verdict}
            chosen_confidence={@chosen_confidence}
            rationale={@rationale}
            submit_error={@submit_error}
            expanded_candidate={@expanded_candidate}
            subject_index={@subject_index}
            subject_answers={@subject_answers}
          />
        <% else %>
          <div class="flex-1 flex items-center justify-center">
            <div class="app-card p-8 max-w-md text-center">
              <div class="text-sm opacity-70">
                <%= if @db_present do %>
                  No pending reviews. Generate pairs from a domain, or check the
                  <.link navigate={DomainNav.results_path(@domain_filter)} class="underline">
                    results
                  </.link>
                  page for past ratings.
                <% else %>
                  The judgement-fabric DB hasn't been initialized yet.
                  Run <code class="font-mono text-xs">python3 bin/judgement.py init</code>
                  from the project root.
                <% end %>
              </div>
            </div>
          </div>
        <% end %>
      </main>
    </.workspace_split>
    """
  end

  attr :active, :map, required: true
  attr :judge_brief, :string, required: true
  attr :judge_brief_loading, :boolean, required: true
  attr :chosen_verdict, :any, required: true
  attr :chosen_confidence, :string, required: true
  attr :rationale, :string, required: true
  attr :submit_error, :any, required: true
  attr :expanded_candidate, :any, required: true
  attr :subject_index, :integer, required: true
  attr :subject_answers, :map, required: true

  defp judge_pane(assigns) do
    payload = assigns.active.trace_payload || %{}
    rubric = Verdicts.normalize_rubric(assigns.active.output_definition || %{})
    single? = rubric.kind == "single"

    # Single judgements carry one artifact under "card"; handle payloads
    # that still shipped "card_a" defensively.
    left =
      if single?,
        do: display_item(payload, single_card_key(payload), "input_a"),
        else: display_item(payload, "card_a", "input_a")

    right =
      if single?,
        do: %{present?: false, title: nil, body: nil, source_ref: nil},
        else: display_item(payload, "card_b", "input_b")

    subject_index = min(assigns.subject_index, length(rubric.subjects) - 1)
    active_subject = Enum.at(rubric.subjects, subject_index)

    assigns =
      assigns
      |> assign(:left_item, left)
      |> assign(:right_item, right)
      |> assign(:pair_labels, pair_labels(payload))
      |> assign(:cited_evidence, resolve_cited_evidence(payload))
      |> assign(:pair_title, pair_title(left, right, payload))
      |> assign(:artifact_kind, artifact_kind(left, right))
      |> assign(:rubric, rubric)
      |> assign(:single?, single?)
      |> assign(:subject_index, subject_index)
      |> assign(:active_subject, active_subject)
      |> assign(:multi_subject?, length(rubric.subjects) > 1)
      |> assign(:last_subject?, subject_index == length(rubric.subjects) - 1)

    ~H"""
    <header class="px-6 py-4 bg-base-100/80 backdrop-blur border-b app-hairline">
      <div class="flex items-baseline justify-between gap-4">
        <div class="min-w-0">
          <h1 class="text-base font-semibold tracking-tight leading-snug" title={@pair_title}>
            {@pair_title}
          </h1>
          <p class="text-xs opacity-60 mt-0.5 font-mono truncate">
            Pair {Map.get(@active.trace_payload, "label") || "##{@active.match_id}"} · {@active.domain_name ||
              Path.basename(@active.tournament_db_path, ".db")} · {@active.template_name} v{@active.template_version}
            <span class={[
              "ml-1.5 px-1.5 py-0.5 rounded text-[10px] font-semibold not-italic",
              @artifact_kind == :work_order && "bg-primary/15 text-primary",
              @artifact_kind == :legacy_card && "bg-base-200 opacity-80"
            ]}>
              {if @artifact_kind == :work_order, do: "Work orders", else: "Legacy cards"}
            </span>
          </p>
        </div>
        <div class="flex gap-2 shrink-0">
          <button phx-click="skip" class="btn btn-ghost btn-sm">Skip</button>
        </div>
      </div>
    </header>

    <div class="flex-1 overflow-auto p-6 space-y-5">
      <details id="judging-brief" class="app-card overflow-hidden" open>
        <summary class="px-4 py-3 cursor-pointer flex items-center gap-2 hover:bg-base-200/50 transition">
          <.icon name="hero-scale" class="size-4 text-primary" />
          <span class="text-sm font-semibold">Judging brief</span>
          <span class="text-xs opacity-50 ml-auto">
            <%= if @judge_brief_loading do %>
              loading full brief…
            <% else %>
              use this lens for both candidates
            <% end %>
          </span>
        </summary>
        <div class="px-4 pb-4 pt-3 border-t app-hairline whitespace-pre-wrap text-sm leading-6 opacity-80">
          {@judge_brief}
        </div>
      </details>

      <%= if @expanded_candidate do %>
        <% {exp_label, exp_item} =
          if @expanded_candidate == "left",
            do: {@pair_labels.left, @left_item},
            else: {@pair_labels.right, @right_item} %>
        <div class="app-card p-6" id="candidate-full">
          <div class="flex items-center justify-between gap-3 mb-3">
            <div class="text-xs uppercase tracking-widest opacity-60">
              {exp_label} — full document
            </div>
            <button
              phx-click="expand_candidate"
              phx-value-side={@expanded_candidate}
              class="btn btn-ghost btn-xs"
            >
              ← back to side-by-side
            </button>
          </div>
          <.judge_item_card
            label={exp_label}
            item={exp_item}
            side={@expanded_candidate}
            expanded={true}
            pending_id={@active.id}
          />
        </div>
      <% else %>
        <%= if @single? do %>
          <div id="single-artifact">
            <.judge_item_card
              label={@pair_labels.left}
              item={@left_item}
              side="left"
              expanded={false}
              pending_id={@active.id}
            />
          </div>
        <% else %>
          <div class="grid grid-cols-2 gap-3">
            <.judge_item_card
              label={@pair_labels.left}
              item={@left_item}
              side="left"
              expanded={false}
              pending_id={@active.id}
            />
            <.judge_item_card
              label={@pair_labels.right}
              item={@right_item}
              side="right"
              expanded={false}
              pending_id={@active.id}
            />
          </div>
        <% end %>
      <% end %>

      <%= if Map.get(@active.trace_payload, "winner_id") do %>
        <div class="app-card p-4">
          <div class="text-xs uppercase tracking-widest opacity-60 mb-1">Agent's pick</div>
          <div class="text-sm">
            <span class="font-semibold">input_{Map.get(@active.trace_payload, "winner_id")}</span>
            <%= if reasoning = Map.get(@active.trace_payload, "winner_reasoning") do %>
              <span class="opacity-70">— {reasoning}</span>
            <% end %>
          </div>
        </div>
      <% end %>

      <%= if synthesis = Map.get(@active.trace_payload, "synthesis") || Map.get(@active.trace_payload, "conclusion") do %>
        <div class="app-card p-4">
          <div class="text-xs uppercase tracking-widest opacity-60 mb-2">Agent's synthesis</div>
          <div class="prose prose-sm max-w-none">
            {SafeMarkdown.render(synthesis)}
          </div>
        </div>
      <% end %>

      <%= if @cited_evidence != [] do %>
        <div class="app-card p-4" id="cited-evidence">
          <div class="text-xs uppercase tracking-widest opacity-60 mb-2">Cited evidence</div>
          <ul class="space-y-2">
            <li :for={ev <- @cited_evidence} class="flex items-start gap-2 text-sm min-w-0">
              <span class={[
                "text-[10px] font-semibold px-1.5 py-0.5 rounded shrink-0 mt-0.5",
                evidence_tier_class(ev.trust_tier)
              ]}>
                {evidence_tier_label(ev.trust_tier)}
              </span>
              <span class="text-[10px] px-1.5 py-0.5 rounded bg-base-200 opacity-70 shrink-0 mt-0.5">
                {ev.kind}
              </span>
              <span class="min-w-0">
                <span class="opacity-80">{evidence_excerpt_line(ev)}</span>
                <a
                  :if={SafeMarkdown.safe_link?(ev.browsable_url)}
                  href={ev.browsable_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  class="ml-1.5 inline-flex items-center gap-1 text-[11px] font-mono opacity-60 hover:opacity-100 underline"
                  title={ev.browsable_url}
                >
                  <.icon name="hero-link" class="size-3" /> source
                </a>
                <span class="block font-mono text-[10px] opacity-40 truncate" title={ev.digest}>
                  {String.slice(ev.digest, 0, 12)}
                </span>
              </span>
            </li>
          </ul>
        </div>
      <% end %>

      <div class="app-card p-6">
        <div class="mb-4 flex items-start justify-between gap-4">
          <div>
            <h2 class="font-semibold text-base">Your verdict</h2>
            <p class="text-xs opacity-60 mt-0.5">
              <%= if @single? do %>
                Judge this artifact on its own merits. Rationale is optional but valuable when the call is close.
              <% else %>
                Decide which candidate better satisfies the judging brief. Rationale is optional but valuable when the choice is close.
              <% end %>
            </p>
          </div>
          <Verdicts.subject_stepper
            :if={@multi_subject?}
            subjects={@rubric.subjects}
            index={@subject_index}
          />
        </div>

        <form
          phx-change="change"
          phx-submit={if @multi_subject?, do: "submit_judgement", else: "submit"}
          id="judge-form"
          class="space-y-5"
        >
          <%= if @multi_subject? do %>
            <%= for {subject, answer} <- @subject_answers, is_map(answer) do %>
              <input type="hidden" name={"subjects[#{subject}][verdict]"} value={answer["verdict"]} />
              <input
                type="hidden"
                name={"subjects[#{subject}][confidence]"}
                value={answer["confidence"]}
              />
              <input
                type="hidden"
                name={"subjects[#{subject}][rationale]"}
                value={answer["rationale"]}
              />
            <% end %>
            <input
              type="hidden"
              name={"subjects[#{@active_subject}][verdict]"}
              value={@chosen_verdict}
            />
            <input
              type="hidden"
              name={"subjects[#{@active_subject}][confidence]"}
              value={@chosen_confidence}
            />
            <input
              type="hidden"
              name={"subjects[#{@active_subject}][rationale]"}
              value={@rationale}
            />
          <% end %>

          <div>
            <div class="label pb-1">
              <span class="label-text text-xs uppercase tracking-wider opacity-70">Verdict</span>
            </div>
            <%= cond do %>
              <% @rubric.wheel && @single? -> %>
                <Verdicts.verdict_axis
                  wheel={@rubric.wheel}
                  chosen={@chosen_verdict}
                  subject={@active_subject}
                />
                <Verdicts.operational_verdicts
                  verdicts={@rubric.operational}
                  chosen={@chosen_verdict}
                />
              <% @rubric.wheel -> %>
                <Verdicts.verdict_wheel
                  wheel={@rubric.wheel}
                  chosen={@chosen_verdict}
                  subject={@active_subject}
                />
                <Verdicts.operational_verdicts
                  verdicts={@rubric.operational}
                  chosen={@chosen_verdict}
                />
              <% true -> %>
                <div class="grid grid-cols-2 gap-2">
                  <%= for {v, idx} <- Enum.with_index(@active.output_definition["verdict_enum"]) do %>
                    <button
                      type="button"
                      phx-click="set_verdict"
                      phx-value-v={v}
                      class={[
                        "btn btn-sm justify-start gap-2 normal-case",
                        @chosen_verdict == v && "btn-primary",
                        @chosen_verdict != v && "btn-ghost border app-hairline"
                      ]}
                    >
                      <span class="text-[10px] font-mono opacity-50 w-4 text-center">{idx + 1}</span>
                      <span class="text-xs font-mono opacity-70">{verdict_glyph(v)}</span>
                      <span class="truncate">{verdict_label(v)}</span>
                    </button>
                  <% end %>
                </div>
            <% end %>
          </div>

          <div>
            <div class="label pb-1">
              <span class="label-text text-xs uppercase tracking-wider opacity-70">Confidence</span>
            </div>
            <div class="inline-flex rounded-lg border app-hairline p-0.5">
              <%= for c <- @active.output_definition["confidence_enum"] || ~w(low mid high) do %>
                <button
                  type="button"
                  phx-click="set_confidence"
                  phx-value-v={c}
                  class={[
                    "px-4 py-1.5 rounded-md text-sm font-medium transition cursor-pointer",
                    @chosen_confidence == c && "bg-primary text-primary-content shadow-sm",
                    @chosen_confidence != c && "hover:bg-base-200"
                  ]}
                >
                  {c}
                </button>
              <% end %>
            </div>
          </div>

          <div>
            <div class="label pb-1">
              <span class="label-text text-xs uppercase tracking-wider opacity-70">
                Rationale <span class="opacity-50 normal-case">(optional)</span>
              </span>
            </div>
            <textarea
              name="rationale"
              class="textarea textarea-bordered prompt-editor w-full h-24"
              placeholder="What made you decide? 1–3 sentences. Empty is fine if the verdict is obvious."
            >{@rationale}</textarea>
          </div>

          <%= if @submit_error do %>
            <div class="alert alert-error text-sm">{@submit_error}</div>
          <% end %>

          <div class="flex items-center justify-between gap-3 pt-2 border-t app-hairline">
            <div class="text-xs opacity-50 font-mono">
              <%= if @rubric.wheel do %>
                <span class="kbd kbd-xs">1-9</span>
                compass (numpad) · <span class="kbd kbd-xs">space</span>
                submit · <span class="kbd kbd-xs">S</span>
                skip · <span class="kbd kbd-xs">J</span>/<span class="kbd kbd-xs">K</span> nav
              <% else %>
                <span class="kbd kbd-xs">1-{length(@active.output_definition["verdict_enum"])}</span>
                verdict · <span class="kbd kbd-xs">space</span>
                submit · <span class="kbd kbd-xs">S</span>
                skip · <span class="kbd kbd-xs">J</span>/<span class="kbd kbd-xs">K</span> nav
              <% end %>
            </div>
            <div class="flex items-center gap-3">
              <span class="text-xs opacity-60">
                <%= if @chosen_verdict do %>
                  <span class="font-mono">{@chosen_verdict}</span> ({@chosen_confidence})
                <% else %>
                  Pick a verdict to submit.
                <% end %>
              </span>
              <%= if @multi_subject? and not @last_subject? do %>
                <button
                  type="button"
                  id="next-subject"
                  phx-click="next_subject"
                  class="btn btn-primary btn-sm"
                  disabled={!@chosen_verdict}
                >
                  Next subject →
                </button>
              <% else %>
                <button type="submit" class="btn btn-primary btn-sm" disabled={!@chosen_verdict}>
                  Submit
                </button>
              <% end %>
            </div>
          </div>
        </form>
      </div>
    </div>
    """
  end

  attr :label, :string, required: true
  attr :item, :map, required: true
  attr :side, :string, required: true
  attr :expanded, :boolean, default: false
  attr :pending_id, :integer, required: true

  defp judge_item_card(assigns) do
    ~H"""
    <div class="app-card p-4 min-h-40">
      <div class="flex items-center gap-2 mb-2">
        <div class="text-xs uppercase tracking-widest opacity-60">{@label}</div>
        <%= if @item[:work_order] do %>
          <span class={[
            "text-[10px] font-semibold px-1.5 py-0.5 rounded",
            priority_class(@item.work_order["priority"])
          ]}>
            {@item.work_order["priority"]}
          </span>
          <span class="text-[10px] px-1.5 py-0.5 rounded bg-base-200 opacity-70">
            {@item.work_order["work_type"]}
          </span>
        <% end %>
        <div class="ml-auto flex items-center gap-1">
          <button
            :if={@item.present? and not @expanded}
            phx-click="expand_candidate"
            phx-value-side={@side}
            class="btn btn-ghost btn-xs gap-1"
            id={"expand-#{@side}"}
            title="Read this candidate at full width, without the side-by-side squeeze"
          >
            <.icon name="hero-arrows-pointing-out" class="size-3" /> Read full
          </button>
          <.link
            :if={@item.present?}
            navigate={candidate_path(@pending_id, @side)}
            class="btn btn-ghost btn-xs gap-1"
            id={"open-#{@side}"}
            title="Open this candidate at its own shareable URL"
          >
            <.icon name="hero-arrow-top-right-on-square" class="size-3" /> Open ↗
          </.link>
        </div>
      </div>
      <%= if @item.present? do %>
        <div class="font-semibold text-sm leading-snug">{@item.title}</div>
        <%= if @item[:work_order] do %>
          <%= if links = @item.work_order["links"] do %>
            <% links = Enum.filter(links, &SafeMarkdown.safe_link?(&1["url"])) %>
            <div :if={links != []} class="flex flex-wrap gap-1.5 mt-2">
              <a
                :for={link <- links}
                href={link["url"]}
                target="_blank"
                rel="noopener noreferrer"
                class="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded-full border app-hairline bg-base-200/40 hover:bg-base-200 transition font-mono"
                title={link["url"]}
              >
                <.icon name="hero-link" class="size-3 opacity-60" />
                {link["label"]}
              </a>
            </div>
          <% end %>
          <div class={[
            "prose prose-sm max-w-none mt-3 opacity-90",
            !@expanded && "max-h-[28rem] overflow-y-auto pr-1"
          ]}>
            {SafeMarkdown.render(@item.body)}
          </div>
        <% else %>
          <div class={[
            "text-sm opacity-80 mt-3 whitespace-pre-wrap leading-relaxed",
            !@expanded && "max-h-[28rem] overflow-y-auto pr-1"
          ]}>
            {@item.body}
          </div>
        <% end %>
        <%= if @item.source_ref do %>
          <div class="font-mono text-[10px] opacity-50 truncate mt-4" title={@item.source_ref}>
            {@item.source_ref}
          </div>
        <% end %>
      <% else %>
        <div class="text-xs opacity-50 italic">— bye / no input —</div>
      <% end %>
    </div>
    """
  end

  @doc "Priority badge class — shared with CandidateLive."
  def priority_class("P0"), do: "bg-error/20 text-error"
  def priority_class("P1"), do: "bg-warning/20 text-warning"
  def priority_class(_), do: "bg-base-200 opacity-70"

  # ── Cited evidence (EvidenceRef digests → catalog rows) ───────────────
  #
  # Wave-3 wiring will stamp evidence digests onto WorkOrder payloads; until
  # then the payload key is `cited_evidence` (a list of hex digests, either
  # top-level or per work-order card). Unresolvable digests and absent keys
  # render nothing — the section is strictly additive.

  @doc """
  Resolve payload `cited_evidence` digests to catalog rows — shared with
  CandidateLive. Unresolvable digests and absent keys render nothing.
  """
  def resolve_cited_evidence(payload) do
    payload
    |> cited_digests()
    |> Enum.uniq()
    |> Enum.map(&TournamentUi.Catalog.get_evidence/1)
    |> Enum.reject(&is_nil/1)
  end

  defp cited_digests(payload) do
    top = as_digest_list(Map.get(payload, "cited_evidence"))

    per_card =
      ["card_a", "card_b"]
      |> Enum.flat_map(fn key ->
        case Map.get(payload, key) do
          card when is_map(card) -> as_digest_list(Map.get(card, "cited_evidence"))
          _ -> []
        end
      end)

    top ++ per_card
  end

  defp as_digest_list(list) when is_list(list), do: Enum.filter(list, &is_binary/1)
  defp as_digest_list(_), do: []

  @doc "Evidence tier badge class — shared with CandidateLive."
  def evidence_tier_class(3), do: "bg-error/20 text-error"
  def evidence_tier_class(2), do: "bg-warning/15 text-warning"
  def evidence_tier_class(_), do: "bg-success/15 text-success"

  @doc "Evidence tier label — shared with CandidateLive."
  def evidence_tier_label(1), do: "TIER1"
  def evidence_tier_label(2), do: "TIER2"
  def evidence_tier_label(3), do: "TIER3 · UNTRUSTED"
  def evidence_tier_label(other), do: "TIER#{other}"

  defp evidence_excerpt_line(ev) do
    (ev.excerpt || ev.summary || "")
    |> String.split("\n", parts: 2)
    |> List.first()
  end

  @doc """
  Build the render map for one side of a pair payload. Public because
  CandidateLive (the standalone permalink page) reuses it — comparison
  and standalone rendering must not drift.
  """
  def display_item(payload, card_key, legacy_path_key) do
    cond do
      is_map(Map.get(payload, card_key)) ->
        card = Map.get(payload, card_key)

        %{
          present?: true,
          title: Map.get(card, "title") || "(untitled card)",
          body: Map.get(card, "body") || "",
          source_ref: Map.get(card, "source_ref"),
          work_order: if(Map.get(card, "kind") == "work-order", do: Map.get(card, "work_order"))
        }

      is_binary(Map.get(payload, legacy_path_key)) ->
        path = Map.get(payload, legacy_path_key)

        %{
          present?: true,
          title: Path.basename(path),
          body: path,
          source_ref: path
        }

      true ->
        %{present?: false, title: nil, body: nil, source_ref: nil}
    end
  end

  # SingleJudgement payloads carry the artifact under "card"; tolerate
  # payloads that still shipped "card_a" (defensive per the contract).
  defp single_card_key(payload) do
    if is_map(Map.get(payload, "card")), do: "card", else: "card_a"
  end

  defp pair_labels(payload) do
    cond do
      is_map(Map.get(payload, "card")) ->
        %{left: "Artifact", right: nil}

      get_in(payload, ["card_a", "kind"]) == "work-order" or
          get_in(payload, ["card_b", "kind"]) == "work-order" ->
        %{left: "Work order A", right: "Work order B"}

      Map.has_key?(payload, "card_a") or Map.has_key?(payload, "card_b") ->
        %{left: "Card A", right: "Card B"}

      true ->
        %{left: "Input 1", right: "Input 2"}
    end
  end

  # The headline a judge actually needs: what is being compared — the two
  # candidate titles — not our bookkeeping (pair label, rubric, version),
  # which moves to the metadata line. (User: "what is someone expected to
  # learn from that title?")
  defp pair_title(%{present?: true, title: a}, %{present?: true, title: b}, _payload)
       when is_binary(a) and is_binary(b) do
    "#{truncate_title(a)}  vs  #{truncate_title(b)}"
  end

  defp pair_title(%{present?: true, title: a}, _right, _payload) when is_binary(a) do
    truncate_title(a)
  end

  defp pair_title(_left, %{present?: true, title: b}, _payload) when is_binary(b) do
    truncate_title(b)
  end

  defp pair_title(_left, _right, payload) do
    Map.get(payload, "label") || "Untitled pair"
  end

  defp truncate_title(title) do
    if String.length(title) > 90, do: String.slice(title, 0, 87) <> "…", else: title
  end

  @doc """
  Shareable permalink for one candidate of a pair. `side` is the UI
  position ("left"/"right") or the payload key suffix ("a"/"b") — both
  normalize to the canonical /candidates/:id/:side URL.
  """
  def candidate_path(pending_id, side) do
    "/candidates/#{pending_id}/#{normalize_side(side)}"
  end

  defp normalize_side("left"), do: "a"
  defp normalize_side("right"), do: "b"
  defp normalize_side(side) when side in ["a", "b"], do: side

  defp artifact_kind(left, right) do
    if left[:work_order] || right[:work_order], do: :work_order, else: :legacy_card
  end

  # Sidebar row headline: lead with what the pair is ABOUT (first candidate
  # title), not the pair label — R1-1 moves to the secondary line.
  defp row_headline(row) do
    payload = row.trace_payload || %{}

    title =
      get_in(payload, ["card_a", "title"]) || get_in(payload, ["card_b", "title"])

    case title do
      t when is_binary(t) and t != "" -> t
      _ -> Map.get(payload, "label") || "match #{row.match_id}"
    end
  end

  defp verdict_glyph("a-clearly-better"), do: "⬅︎⬅︎"
  defp verdict_glyph("a-marginally-better"), do: "⬅︎"
  defp verdict_glyph("tie-both-strong"), do: "≡+"
  defp verdict_glyph("tie-both-weak"), do: "≡-"
  defp verdict_glyph("b-marginally-better"), do: "➡︎"
  defp verdict_glyph("b-clearly-better"), do: "➡︎➡︎"
  defp verdict_glyph("incoherent"), do: "✗"
  defp verdict_glyph("skip"), do: "↻"
  defp verdict_glyph(_), do: "•"

  defp verdict_label("a-clearly-better"), do: "Input 1 (clearly)"
  defp verdict_label("a-marginally-better"), do: "Input 1 (marginally)"
  defp verdict_label("tie-both-strong"), do: "Tie — both strong"
  defp verdict_label("tie-both-weak"), do: "Tie — both weak"
  defp verdict_label("b-marginally-better"), do: "Input 2 (marginally)"
  defp verdict_label("b-clearly-better"), do: "Input 2 (clearly)"
  defp verdict_label("incoherent"), do: "Incoherent"
  defp verdict_label("skip"), do: "Skip"
  defp verdict_label(other), do: other
end
