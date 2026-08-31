defmodule TournamentUiWeb.DomainsLive do
  @moduledoc """
  /domains — list active domains, kick off card generation per domain,
  link to /domains/new for creating new ones.

  ## One primary action per card, and the chip cannot disagree with it

  `domain_stage/1` returns the stage chip AND the single call to action, so
  the badge saying "Human review" and the highlighted button saying "Review 3"
  are the same decision rendered twice. Two `btn-primary` controls on one card
  — a "Generate pairs" that spawns a job beside a "Review N" that only
  navigates — asked the operator to guess which of the two was the next thing
  to do, and made the destructive-ish one look equally inviting on a domain
  that had already been generated. Everything else on the card is a ghost
  affordance: still reachable, never competing.

  The settled order is a first-class destination here. A domain whose pairs
  have been judged points at `/standings`, because that is what the judging
  bought.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.{Domains, OptimizerRunner, OptimizerRuns}
  alias TournamentUiWeb.DomainNav

  @generator_script System.get_env("GENERATE_CARDS_SCRIPT") ||
                      Path.expand("../../../../../bin/generate_cards.py", __ENV__.file)
  @optimizer_script System.get_env("OPTIMIZE_SCRIPT") ||
                      Path.expand("../../../../../bin/optimize.py", __ENV__.file)
  @repo_root System.get_env("DATA_TOURNAMENTS_REPO") ||
               Path.expand("../../../../..", __ENV__.file)

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket) do
      :timer.send_interval(5_000, :refresh)
      Phoenix.PubSub.subscribe(TournamentUi.PubSub, OptimizerRunner.topic())
    end

    {:ok,
     socket
     |> assign(domains: [], log_lines: [], active_job: nil)
     |> restore_job()
     |> refresh()}
  end

  # Re-attach to a job started before this LiveView mounted (jobs live in
  # the OptimizerRunner registry, not in socket assigns, so navigating away
  # and back must not lose a running generation or its log tail).
  defp restore_job(socket) do
    case OptimizerRunner.latest_job(:domains) do
      %{status: :running, meta: meta, lines: lines} ->
        assign(socket, active_job: Map.take(meta, [:kind, :domain]), log_lines: lines)

      %{status: :finished, lines: lines} when lines != [] ->
        assign(socket, log_lines: lines)

      _ ->
        socket
    end
  end

  @impl true
  def handle_info(:refresh, socket), do: {:noreply, refresh(socket)}

  def handle_info({:optimizer_line, _lock, %{source: :domains}, line}, socket) do
    {:noreply, update(socket, :log_lines, &Enum.take([line | &1], 200))}
  end

  def handle_info({:optimizer_line, _lock, _meta, _line}, socket), do: {:noreply, socket}

  def handle_info({:optimizer_exit, _lock, %{source: :domains} = meta, status}, socket) do
    {flash_kind, msg} = job_result(Map.take(meta, [:kind, :domain]), status)

    {:noreply,
     socket
     |> assign(active_job: nil)
     |> put_flash(flash_kind, msg)
     |> refresh()}
  end

  def handle_info({:optimizer_exit, _lock, _meta, _status}, socket), do: {:noreply, socket}

  @impl true
  def handle_event("generate", %{"name" => name}, socket) do
    case OptimizerRunner.start(
           "python3",
           [@generator_script, "--domain", name],
           rubric_lock: "generate:#{name}",
           meta: %{source: :domains, kind: :fan_out, domain: name},
           cd: @repo_root
         ) do
      {:ok, _pid} ->
        {:noreply,
         socket
         |> assign(active_job: %{kind: :fan_out, domain: name}, log_lines: [])
         |> put_flash(:info, "Generating cards for #{name}…")}

      {:error, :already_running} ->
        {:noreply, put_flash(socket, :error, "Generation already running for #{name}.")}

      {:error, reason} ->
        {:noreply, put_flash(socket, :error, "Failed: #{inspect(reason)}")}
    end
  end

  @impl true
  def handle_event("optimize_judge", %{"name" => name}, socket) do
    case Enum.find(socket.assigns.domains, &(&1.name == name)) do
      nil ->
        {:noreply, put_flash(socket, :error, "Unknown domain #{name}.")}

      domain ->
        run_id =
          OptimizerRuns.start(
            domain: name,
            target: "judge",
            rubric: domain.rubric,
            prompt_name: domain.judge_prompt
          )

        args = [
          @optimizer_script,
          "--rubric",
          domain.rubric,
          "--domain",
          name,
          "--prompt-name",
          domain.judge_prompt,
          "--max-metric-calls",
          "40",
          "--run-id",
          Integer.to_string(run_id)
        ]

        case OptimizerRunner.start(
               "python3",
               args,
               rubric_lock: "optimize:#{domain.rubric}:#{name}",
               meta: %{source: :domains, kind: :optimize, domain: name},
               cd: @repo_root
             ) do
          {:ok, _pid} ->
            {:noreply,
             socket
             |> assign(active_job: %{kind: :optimize, domain: name}, log_lines: [])
             |> put_flash(:info, "Improving #{name}'s judge from its human verdicts…")}

          {:error, :already_running} ->
            {:noreply,
             put_flash(socket, :error, "Judge optimization already running for #{name}.")}

          {:error, reason} ->
            {:noreply, put_flash(socket, :error, "Failed: #{inspect(reason)}")}
        end
    end
  end

  defp job_result(%{kind: :fan_out}, 0),
    do: {:info, "Generation finished. Candidate pairs are ready in the review queue."}

  defp job_result(%{kind: :fan_out}, status), do: {:error, "Generation exited #{status}."}

  defp job_result(%{kind: :optimize, domain: domain}, status) do
    latest = OptimizerRuns.latest(domain: domain, target: "judge")

    if (status == 0 and latest) && latest.status == "done" do
      if latest.result && latest.result["accepted"] do
        {:info, "Improved context accepted for #{domain}. Review its candidate on Prompts."}
      else
        {:info, "Context evolution finished for #{domain}; the production seed was retained."}
      end
    else
      reason = if latest && latest.result, do: latest.result["error"], else: nil
      {:error, reason || "Judge optimization failed for #{domain} (exit #{status})."}
    end
  end

  defp job_result(_, status), do: {:error, "Background job exited #{status}."}

  defp job_label(%{kind: :fan_out, domain: domain}), do: "Generating pairs · #{domain}"
  defp job_label(%{kind: :optimize, domain: domain}), do: "Improving rubric · #{domain}"
  defp job_label(_), do: "Recent job"

  defp refresh(socket), do: assign(socket, domains: Domains.list())

  defp domain_stage(d) do
    cond do
      d.error_count > 0 ->
        stage(
          :results,
          "Needs attention",
          "bg-error/15 text-error",
          "Inspect failed ratings →",
          d
        )

      d.match_count == 0 ->
        stage(
          :generate,
          "Ready to generate",
          "bg-base-200 text-base-content/70",
          "Generate pairs",
          d
        )

      d.pending_human > 0 ->
        stage(
          :judge,
          "Human review",
          "bg-primary/15 text-primary",
          "Review #{d.pending_human} →",
          d
        )

      d.pending_llm > 0 ->
        stage(:standings, "Model review", "bg-info/15 text-info", "See the priority order →", d)

      true ->
        stage(
          :standings,
          "Results ready",
          "bg-success/15 text-success",
          "See the priority order →",
          d
        )
    end
  end

  defp stage(key, label, class, action_label, d),
    do: %{
      key: key,
      label: label,
      class: class,
      action_label: action_label,
      action_href: href_for(key, d)
    }

  defp href_for(:generate, _d), do: nil
  defp href_for(:judge, d), do: DomainNav.judge_path(d.name)
  defp href_for(:results, d), do: DomainNav.results_path(d.name)
  defp href_for(:standings, d), do: standings_path(d.name)

  defp standings_path(domain), do: "/standings?" <> URI.encode_query(%{"domain" => domain})

  defp secondary_actions(d, stage) do
    [
      d.pending_human > 0 &&
        %{key: :judge, label: "Review #{d.pending_human} →", href: DomainNav.judge_path(d.name)},
      d.match_count > 0 &&
        %{key: :standings, label: "Priority order →", href: standings_path(d.name)},
      d.match_count > 0 &&
        %{key: :results, label: "Compare results →", href: DomainNav.results_path(d.name)}
    ]
    |> Enum.filter(&is_map/1)
    |> Enum.reject(&(&1.key == stage.key))
  end

  defp completed_count(d), do: d.completed_human + d.completed_llm
  defp total_count(d), do: completed_count(d) + d.pending_human + d.pending_llm + d.error_count

  defp completion_percent(d) do
    case total_count(d) do
      0 -> 0
      total -> round(completed_count(d) * 100 / total)
    end
  end

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:domains}
      flash={@flash}
      title="Domains"
      subtitle="Your workflow hubs: configure a source and lens, generate candidate pairs, review them, then compare results."
    >
      <:title_actions>
        <.link navigate="/judge" class="btn btn-ghost btn-sm">Review queue</.link>
        <.link navigate="/start" class="btn btn-primary btn-sm">+ New domain</.link>
      </:title_actions>

      <%= if @domains == [] do %>
        <div class="app-card p-8 text-center space-y-4">
          <div class="text-sm opacity-70">
            No domains yet. A domain pairs one evaluation category with one
            source corpus, then generates candidates for review.
          </div>
          <.link navigate="/start" id="empty-domains-cta" class="btn btn-primary">
            Choose your first evaluation category →
          </.link>
        </div>
      <% else %>
        <div class="space-y-4">
          <%= for d <- @domains do %>
            <% stage = domain_stage(d) %>
            <article class="app-card p-5" id={"domain-#{d.id}"}>
              <div class="flex items-start justify-between gap-4">
                <div class="min-w-0">
                  <div class="flex items-center gap-2 flex-wrap">
                    <.link
                      navigate={"/domains/#{d.name}/edit"}
                      class="font-mono text-sm font-semibold hover:text-primary transition"
                    >
                      {d.name}
                    </.link>
                    <span class={["text-[11px] font-medium px-2 py-0.5 rounded-full", stage.class]}>
                      {stage.label}
                    </span>
                  </div>
                  <div class="text-sm opacity-80 mt-1">{d.description}</div>
                  <div class="text-xs opacity-55 mt-2 flex gap-3 flex-wrap">
                    <span>
                      source:
                      <strong class="font-mono font-medium">
                        {Map.get(d.corpus_source, "kind", "?")}
                      </strong>
                    </span>
                    <span>created {format_date(d.created_at)}</span>
                    <span :if={d.last_activity}>last activity {format_date(d.last_activity)}</span>
                  </div>
                </div>
                <div class="flex gap-2 shrink-0">
                  <.link navigate={"/domains/#{d.name}/edit"} class="btn btn-ghost btn-sm">
                    Configure
                  </.link>
                  <button
                    phx-click="optimize_judge"
                    phx-value-name={d.name}
                    class={["btn btn-sm btn-ghost border app-hairline", @active_job && "btn-disabled"]}
                    disabled={@active_job != nil}
                    title="Requires at least 7 human judgements for train, validation, and holdout"
                  >
                    Improve rubric
                  </button>
                  <button
                    :if={stage.key != :generate}
                    phx-click="generate"
                    phx-value-name={d.name}
                    class={["btn btn-sm btn-ghost border app-hairline", @active_job && "btn-disabled"]}
                    disabled={@active_job != nil}
                  >
                    Regenerate pairs
                  </button>
                </div>
              </div>

              <div class="mt-5 pt-4 border-t app-hairline">
                <div class="grid gap-3 sm:grid-cols-4 text-xs">
                  <.domain_metric label="Pairs" value={d.match_count} detail="generated matches" />
                  <.domain_metric
                    label="Human"
                    value={d.completed_human}
                    detail={"#{d.pending_human} pending"}
                  />
                  <.domain_metric
                    label="Models"
                    value={d.completed_llm}
                    detail={"#{d.pending_llm} pending"}
                  />
                  <.domain_metric label="Errors" value={d.error_count} detail="failed ratings" />
                </div>

                <div :if={total_count(d) > 0} class="mt-4 flex items-center gap-3">
                  <div class="h-1.5 rounded-full bg-base-200 overflow-hidden flex-1">
                    <div class="h-full bg-primary/70" style={"width: #{completion_percent(d)}%"}>
                    </div>
                  </div>
                  <span class="text-[11px] font-mono opacity-55">{completion_percent(d)}% rated</span>
                </div>

                <div class="mt-4 flex items-center justify-end gap-2 flex-wrap">
                  <%= for action <- secondary_actions(d, stage) do %>
                    <.link
                      navigate={action.href}
                      class="btn btn-sm btn-ghost border app-hairline"
                    >
                      {action.label}
                    </.link>
                  <% end %>

                  <button
                    :if={stage.key == :generate}
                    phx-click="generate"
                    phx-value-name={d.name}
                    data-role="primary-action"
                    id={"domain-#{d.id}-primary"}
                    class={["btn btn-sm btn-primary", @active_job && "btn-disabled"]}
                    disabled={@active_job != nil}
                  >
                    {stage.action_label}
                  </button>
                  <.link
                    :if={stage.key != :generate}
                    navigate={stage.action_href}
                    data-role="primary-action"
                    id={"domain-#{d.id}-primary"}
                    class="btn btn-sm btn-primary"
                  >
                    {stage.action_label}
                  </.link>
                </div>
              </div>
            </article>
          <% end %>
        </div>
      <% end %>

      <%= if @log_lines != [] or @active_job do %>
        <div class="app-card mt-6 p-4">
          <div class="flex items-center justify-between mb-2">
            <div class="text-xs uppercase tracking-wider opacity-60">{job_label(@active_job)}</div>
            <div class="text-xs font-mono opacity-50">
              {if @active_job, do: "running…", else: "finished"}
            </div>
          </div>
          <pre class="font-mono text-xs leading-5 max-h-64 overflow-y-auto"><%= for l <- Enum.reverse(@log_lines) do %>{l}
          <% end %></pre>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  attr :label, :string, required: true
  attr :value, :integer, required: true
  attr :detail, :string, required: true

  defp domain_metric(assigns) do
    ~H"""
    <div class="rounded-lg bg-base-200/55 px-3 py-2">
      <div class="uppercase tracking-wider opacity-50 text-[10px]">{@label}</div>
      <div class="mt-0.5 flex items-baseline gap-2">
        <strong class="font-mono text-base">{@value}</strong>
        <span class="opacity-55 truncate">{@detail}</span>
      </div>
    </div>
    """
  end

  defp format_date(nil), do: "—"
  defp format_date(value) when is_binary(value), do: String.slice(value, 0, 10)
end
