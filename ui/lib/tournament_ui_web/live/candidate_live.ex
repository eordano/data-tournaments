defmodule TournamentUiWeb.CandidateLive do
  @moduledoc """
  /candidates/:id/:side — standalone, shareable permalink for ONE
  candidate of a judgement pair (side `a` or `b`).

  Purpose (user request): candidates need their own URL to inspect and
  share independently of the side-by-side judging flow. The link must
  KEEP WORKING after the pair is judged — it fetches by id regardless of
  status (Judgement.get_judgement/1), so a shared link never dies when
  someone votes.

  Rendering reuses JudgeLive's public helpers (display_item,
  priority_class, evidence tier badges, SafeMarkdown) so the comparison
  view and the permalink can never drift.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.Judgement
  alias TournamentUiWeb.DomainNav
  alias TournamentUiWeb.JudgeLive
  alias TournamentUiWeb.SafeMarkdown

  @impl true
  def mount(%{"id" => id_str, "side" => side} = _params, _session, socket)
      when side in ["a", "b"] do
    row =
      case Integer.parse(id_str) do
        {id, ""} -> Judgement.get_judgement(id)
        _ -> nil
      end

    case row do
      nil ->
        {:ok, assign(socket, row: nil, item: nil, side: side, cited: [])}

      row ->
        payload = row.trace_payload || %{}
        item = JudgeLive.display_item(payload, "card_#{side}", "input_#{side}")

        {:ok,
         assign(socket,
           row: row,
           item: item,
           side: side,
           cited: JudgeLive.resolve_cited_evidence(payload)
         )}
    end
  end

  def mount(_params, _session, socket) do
    # Invalid side (or shape) — same friendly not-found, never a crash.
    {:ok, assign(socket, row: nil, item: nil, side: nil, cited: [])}
  end

  @impl true
  def render(%{row: nil} = assigns) do
    ~H"""
    <.workspace_page current={:judge} flash={@flash} max_width="max-w-2xl" title="Candidate not found">
      <div class="app-card p-8 text-center">
        <div class="text-sm font-medium">No such candidate</div>
        <p class="text-xs opacity-60 mt-2">
          The link may be malformed, or the judgement row was removed.
        </p>
        <.link navigate="/judge" class="btn btn-ghost btn-sm mt-4">← Review queue</.link>
      </div>
    </.workspace_page>
    """
  end

  def render(%{item: %{present?: false}} = assigns) do
    ~H"""
    <.workspace_page current={:judge} flash={@flash} max_width="max-w-2xl" title="Empty side">
      <div class="app-card p-8 text-center">
        <div class="text-sm font-medium">This side of the pair is empty (bye)</div>
        <.link navigate="/judge" class="btn btn-ghost btn-sm mt-4">← Review queue</.link>
      </div>
    </.workspace_page>
    """
  end

  def render(assigns) do
    ~H"""
    <.workspace_page current={:judge} flash={@flash} max_width="max-w-3xl" title={@item.title}>
      <:title_actions>
        <.link
          navigate={DomainNav.judge_path(@row.domain_name)}
          class="text-sm opacity-60 hover:opacity-100"
        >
          ← back to comparison
        </.link>
      </:title_actions>

      <div class="app-card p-6">
        <div class="flex flex-wrap items-center gap-2 mb-4">
          <span class={[
            "text-[10px] font-semibold px-1.5 py-0.5 rounded",
            (@item[:work_order] && "bg-primary/15 text-primary") || "bg-base-200 opacity-80"
          ]}>
            {if @item[:work_order], do: "Work order", else: "Legacy card"}
          </span>
          <%= if wo = @item[:work_order] do %>
            <span class={[
              "text-[10px] font-semibold px-1.5 py-0.5 rounded",
              JudgeLive.priority_class(wo["priority"])
            ]}>
              {wo["priority"]}
            </span>
            <span class="text-[10px] px-1.5 py-0.5 rounded bg-base-200 opacity-70">
              {wo["work_type"]}
            </span>
          <% end %>
          <span
            :if={@row.status && @row.status != "pending"}
            class="text-[10px] px-1.5 py-0.5 rounded bg-base-200 opacity-70"
            title="This pair has already been judged; the permalink stays valid."
          >
            pair {@row.status}
          </span>
        </div>

        <%= if wo = @item[:work_order] do %>
          <%= if links = wo["links"] do %>
            <% links = Enum.filter(links, &SafeMarkdown.safe_link?(&1["url"])) %>
            <div :if={links != []} class="flex flex-wrap gap-1.5 mb-4">
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
          <div class="prose prose-sm max-w-none opacity-90">
            {SafeMarkdown.render(@item.body)}
          </div>
        <% else %>
          <div class="text-sm opacity-80 whitespace-pre-wrap leading-relaxed">{@item.body}</div>
        <% end %>

        <%= if @item.source_ref do %>
          <div class="font-mono text-[10px] opacity-50 mt-6" title={@item.source_ref}>
            {@item.source_ref}
          </div>
        <% end %>
      </div>

      <div :if={@cited != []} class="app-card p-5 mt-4" id="candidate-cited-evidence">
        <div class="text-xs uppercase tracking-widest opacity-60 mb-2">Cited evidence</div>
        <ul class="space-y-2">
          <li :for={ev <- @cited} class="text-xs flex items-start gap-2">
            <span class={[
              "px-1.5 py-0.5 rounded font-semibold shrink-0",
              JudgeLive.evidence_tier_class(ev.trust_tier)
            ]}>
              {JudgeLive.evidence_tier_label(ev.trust_tier)}
            </span>
            <span class="opacity-80 break-all">{ev.canonical_uri}</span>
          </li>
        </ul>
      </div>

      <div class="app-card p-4 mt-4 text-xs opacity-60 font-mono" id="candidate-provenance">
        Candidate {String.upcase(@side)} of pair {Map.get(@row.trace_payload || %{}, "label") ||
          "##{@row.match_id}"} · {@row.domain_name || Path.basename(@row.tournament_db_path, ".db")} · judgement #{@row.id}
        <span class="opacity-50">· permalink /candidates/{@row.id}/{@side}</span>
      </div>
    </.workspace_page>
    """
  end
end
