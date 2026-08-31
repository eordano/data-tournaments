defmodule TournamentUiWeb.RunsLive do
  @moduledoc """
  /runs — release workflow runs (the workflow_run Temporal projection).

  Index: one row per run — status chip, workflow id, started/finished,
  stage count. Detail (/runs/:workflow_id): compact stage timeline (one
  line per stage), and — ONLY when the run is awaiting approval — the
  approve/reject panel wired through TournamentUi.Approvals (fail-closed
  authorization → append-only audit → Signal delivery). The approving
  principal is DT_OPERATOR (single-operator deployment); when unset the
  buttons are disabled with a hint. Approval history renders beneath.

  One-click delivery contract: the Approve click must produce BOTH the
  audit row and a CONFIRMED Signal delivery. When the client shell-out
  fails AFTER the audit row was written, a persistent "audit recorded,
  delivery FAILED" banner shows the client's stderr with a "Retry
  delivery" button that re-dispatches ONLY the Signal
  (Approvals.deliver_signal/4 — never a second audit row). On success a
  delivery-status line under the panel confirms "approval delivered
  (signal accepted)" with the client's output.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.Approvals
  alias TournamentUi.FixBranches
  alias TournamentUi.WorkflowRuns

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket), do: :timer.send_interval(5_000, :refresh)

    {:ok,
     assign(socket,
       runs: [],
       run: nil,
       workflow_id: nil,
       operator: operator(),
       reason: "",
       approval_error: nil,
       delivery_status: nil,
       retry_delivery: nil,
       events: [],
       ship: nil,
       open_stages: MapSet.new(),
       show_raw_json: false
     )}
  end

  @impl true
  def handle_params(params, _uri, socket) do
    # :show reads the path segment; :show_q reads ?id= — the query form is
    # what index links emit because colon-bearing workflow ids (release:x:y)
    # break direct GETs of path-segment URLs.
    workflow_id = params["workflow_id"] || params["id"]
    {:noreply, socket |> assign(workflow_id: workflow_id) |> load()}
  end

  @impl true
  def handle_info(:refresh, socket) do
    {:noreply, load(socket)}
  end

  @impl true
  def handle_event("decide", %{"decision" => decision} = params, socket) do
    workflow_id = socket.assigns.workflow_id
    reason = String.trim(params["reason"] || "")
    approved? = decision == "approve"

    case Approvals.submit_decision(workflow_id, approved?, operator(), reason) do
      {:ok, %{delivery: :ok, decision: d, delivery_output: output}} ->
        {:noreply,
         socket
         |> assign(
           approval_error: nil,
           reason: "",
           retry_delivery: nil,
           delivery_status: delivery_status_line(d, output)
         )
         |> put_flash(:info, "Decision recorded: #{d}")
         |> load()}

      {:ok, %{delivery: {:failed, output}, decision: d}} ->
        # Audit row is on disk; ONLY the Signal is missing. Keep everything
        # needed to re-dispatch it without a second audit row.
        {:noreply,
         socket
         |> assign(
           approval_error:
             "Audit recorded, delivery FAILED — decision #{d} is in the " <>
               "audit trail but the Signal was not delivered: #{output}",
           delivery_status: nil,
           retry_delivery: %{approved?: approved?, decision: d, reason: reason}
         )
         |> load()}

      {:error, reason} ->
        {:noreply, socket |> assign(approval_error: "Denied: #{reason}") |> load()}
    end
  end

  @impl true
  def handle_event("retry-delivery", _params, socket) do
    %{approved?: approved?, decision: d, reason: reason} = socket.assigns.retry_delivery

    # Signal ONLY — the audit row from the original click already records
    # the intent; a retry must never write a second one.
    case Approvals.deliver_signal(socket.assigns.workflow_id, approved?, operator(), reason) do
      {:ok, output} ->
        {:noreply,
         socket
         |> assign(
           approval_error: nil,
           retry_delivery: nil,
           delivery_status: delivery_status_line(d, output)
         )
         |> put_flash(:info, "Delivery retried: signal accepted")
         |> load()}

      {:failed, output} ->
        {:noreply,
         socket
         |> assign(
           approval_error: "Audit recorded, delivery FAILED — retry also failed: #{output}"
         )
         |> load()}
    end
  end

  @impl true
  def handle_event("toggle-stage", %{"idx" => idx}, socket) do
    {i, ""} = Integer.parse(idx)

    open =
      if MapSet.member?(socket.assigns.open_stages, i),
        do: MapSet.delete(socket.assigns.open_stages, i),
        else: MapSet.put(socket.assigns.open_stages, i)

    {:noreply, assign(socket, open_stages: open)}
  end

  @impl true
  def handle_event("toggle-raw-json", _params, socket) do
    {:noreply, assign(socket, show_raw_json: not socket.assigns.show_raw_json)}
  end

  # "approval delivered (signal accepted)" / "rejection delivered ..." —
  # confirmed from the client's own output, not assumed.
  defp delivery_status_line(decision, output) do
    noun = if decision == "approved", do: "approval", else: "rejection"
    line = "#{noun} delivered (signal accepted)"
    if output in [nil, ""], do: line, else: "#{line} — #{output}"
  end

  defp operator, do: System.get_env("DT_OPERATOR")

  defp load(%{assigns: %{workflow_id: nil}} = socket) do
    assign(socket, runs: WorkflowRuns.list_runs(), run: nil, events: [], ship: nil)
  end

  defp load(%{assigns: %{workflow_id: workflow_id}} = socket) do
    assign(socket,
      run: WorkflowRuns.get_run(workflow_id),
      events: Approvals.list_events(workflow_id),
      ship: FixBranches.ship_for_workflow(workflow_id),
      operator: operator()
    )
  end

  @impl true
  def render(%{live_action: action} = assigns) when action in [:show, :show_q] do
    ~H"""
    <.workspace_page
      current={:runs}
      flash={@flash}
      title={@workflow_id}
      subtitle="Release workflow run: stage timeline and approval trail."
    >
      <:title_actions>
        <.link navigate="/runs" class="btn btn-ghost btn-sm">← All runs</.link>
      </:title_actions>

      <%= if @run do %>
        <div class="space-y-4">
          <section class="app-card p-5" id="run-summary">
            <div class="flex items-center gap-3 flex-wrap text-sm">
              <.status_chip status={@run.status} />
              <span class="font-mono text-xs opacity-60" title={@run.temporal_run_id}>
                run {String.slice(@run.temporal_run_id, 0, 12)}
              </span>
              <span class="text-xs opacity-55">started {@run.started_at}</span>
              <span :if={@run.finished_at} class="text-xs opacity-55">
                finished {@run.finished_at}
              </span>
            </div>
          </section>

          <section class="app-card p-5" id="run-timeline">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Stages</h2>
              <span class="text-xs font-mono opacity-40">{@run.stage_count}</span>
            </div>
            <%= if @run.stage_history == [] do %>
              <p class="text-sm opacity-60">No stages recorded yet.</p>
            <% else %>
              <ol class="space-y-2">
                <li
                  :for={{entry, idx} <- Enum.with_index(@run.stage_history)}
                  class="border border-base-200 rounded p-3"
                  id={"stage-#{idx}"}
                >
                  <div class="flex items-center gap-2 text-sm font-mono flex-wrap">
                    <span class={["w-2 h-2 rounded-full shrink-0", stage_dot(entry["status"])]}>
                    </span>
                    <span class="font-medium">{entry["stage"]}</span>
                    <span class={["text-xs", stage_status_class(entry["status"])]}>
                      {entry["status"]}
                    </span>
                    <button
                      :if={stage_detail_text(entry)}
                      type="button"
                      phx-click="toggle-stage"
                      phx-value-idx={idx}
                      class="btn btn-ghost btn-xs"
                      id={"stage-detail-toggle-#{idx}"}
                    >
                      {if MapSet.member?(@open_stages, idx), do: "hide detail", else: "detail"}
                    </button>
                    <%= if entry["at"] do %>
                      <span class="text-[10px] opacity-40 ml-auto" id={"stage-at-#{idx}"}>
                        {entry["at"]}
                      </span>
                    <% else %>
                      <span class="text-[10px] opacity-40 ml-auto" id={"stage-at-#{idx}"}>
                        timestamp not recorded
                      </span>
                    <% end %>
                  </div>
                  <%!-- FULL detail, expandable — everything the row carries,
                        escaped, monospace, pre-wrap; string details render
                        verbatim (incl. DRY-RUN labels), maps pretty-print. --%>
                  <pre
                    :if={stage_detail_text(entry) && MapSet.member?(@open_stages, idx)}
                    class="text-xs font-mono whitespace-pre-wrap overflow-auto max-h-96 bg-base-200/60 rounded p-3 mt-2"
                    id={"stage-detail-#{idx}"}
                  >{stage_detail_text(entry)}</pre>
                </li>
              </ol>
            <% end %>
          </section>

          <section :if={@ship} class="app-card p-5" id="ship-linkage">
            <div class="flex items-baseline gap-2 mb-2">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Origin</h2>
            </div>
            <p class="text-sm">
              Shipped from branch
              <.link
                navigate={"/branch-fixes/#{@ship.fix_branch_id}"}
                class="font-mono font-semibold hover:text-primary transition underline"
                id="ship-linkage-link"
              >
                #{@ship.fix_branch_id}
              </.link>
              <span class="text-xs opacity-60 font-mono">
                (tested {@ship.tested_sha}
                <%= if @ship.requested_by do %>
                  , requested by {@ship.requested_by}
                <% end %>)
              </span>
            </p>
          </section>

          <section
            :if={@run.status == "awaiting-approval"}
            class="app-card p-5"
            id="approval-panel"
          >
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Approval</h2>
            </div>

            <div :if={@approval_error} class="alert alert-error text-sm mb-3" id="approval-error">
              <span>{@approval_error}</span>
              <button
                :if={@retry_delivery}
                type="button"
                phx-click="retry-delivery"
                class="btn btn-outline btn-xs"
                id="retry-delivery-button"
              >
                Retry delivery
              </button>
            </div>

            <form phx-submit="decide" id="approval-form" class="space-y-3">
              <div class="text-xs opacity-70">
                <%= if @operator do %>
                  Deciding as <span class="font-mono font-semibold">{@operator}</span>
                <% else %>
                  No operator identity — set <code class="font-mono">DT_OPERATOR</code>
                  to enable approval.
                <% end %>
              </div>
              <input
                type="text"
                name="reason"
                value={@reason}
                placeholder="Reason (optional)"
                class="input input-sm input-bordered w-full font-mono text-xs"
                id="approval-reason"
              />
              <div class="flex gap-2">
                <button
                  type="submit"
                  name="decision"
                  value="approve"
                  class="btn btn-success btn-sm"
                  disabled={is_nil(@operator)}
                  id="approve-button"
                >
                  Approve
                </button>
                <button
                  type="submit"
                  name="decision"
                  value="reject"
                  class="btn btn-error btn-sm"
                  disabled={is_nil(@operator)}
                  id="reject-button"
                >
                  Reject
                </button>
              </div>
            </form>
          </section>

          <p
            :if={@delivery_status}
            class="text-xs text-success font-mono px-1"
            id="delivery-status"
          >
            {@delivery_status}
          </p>

          <section :if={@events != []} class="app-card p-5" id="approval-history">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Approval history</h2>
              <span class="text-xs font-mono opacity-40">{length(@events)}</span>
            </div>
            <ul class="space-y-1">
              <li :for={ev <- @events} class="flex items-center gap-2 text-sm">
                <span class={[
                  "text-[10px] font-semibold px-1.5 py-0.5 rounded",
                  decision_class(ev.decision)
                ]}>
                  {ev.decision}
                </span>
                <span class="font-mono text-xs">{ev.approver}</span>
                <span :if={ev.reason != ""} class="text-xs opacity-70">— {ev.reason}</span>
                <span class="text-[10px] opacity-40 ml-auto">{ev.created_at}</span>
              </li>
            </ul>
          </section>

          <section class="app-card p-5" id="raw-json-section">
            <button
              type="button"
              phx-click="toggle-raw-json"
              class="btn btn-ghost btn-xs"
              id="raw-json-toggle"
            >
              {if @show_raw_json, do: "hide raw status JSON", else: "raw status JSON"}
            </button>
            <pre
              :if={@show_raw_json}
              class="text-xs font-mono whitespace-pre-wrap overflow-auto max-h-96 bg-base-200/60 rounded p-3 mt-2"
              id="raw-json"
            >{raw_status_json(@run)}</pre>
          </section>
        </div>
      <% else %>
        <div class="app-card p-8 text-center">
          <div class="text-sm opacity-70">
            No run recorded for <strong class="font-mono">{@workflow_id}</strong>.
          </div>
          <.link navigate="/runs" class="btn btn-ghost btn-sm mt-4">Back to runs</.link>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:runs}
      flash={@flash}
      title="Runs"
      subtitle="Release workflow executions: Temporal projection with approval trail."
    >
      <%= if @runs == [] do %>
        <div class="app-card p-8 text-center" id="runs-empty">
          <div class="text-sm opacity-70">
            No workflow runs recorded yet. Runs appear when a release workflow
            starts: <code class="font-mono text-xs">python3 -m bin.release_workflow.client start</code>.
          </div>
        </div>
      <% else %>
        <div class="space-y-2">
          <article :for={run <- @runs} class="app-card px-5 py-3" id={"run-#{run.id}"}>
            <div class="flex items-center gap-3 flex-wrap">
              <.status_chip status={run.status} />
              <.link
                navigate={"/runs/show?id=#{URI.encode_www_form(run.temporal_workflow_id)}"}
                class="font-mono text-sm font-semibold hover:text-primary transition"
              >
                {run.temporal_workflow_id}
              </.link>
              <span class="text-xs opacity-55 ml-auto">
                {run.stage_count} stages · started {run.started_at}
                <%= if run.finished_at do %>
                  · finished {run.finished_at}
                <% end %>
              </span>
            </div>
          </article>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  attr :status, :string, required: true

  defp status_chip(assigns) do
    ~H"""
    <span class={[
      "text-[11px] font-medium px-2 py-0.5 rounded-full",
      chip_class(@status)
    ]}>
      {@status}
    </span>
    """
  end

  defp chip_class("running"), do: "bg-info/15 text-info"
  defp chip_class("awaiting-approval"), do: "bg-warning/20 text-warning"
  defp chip_class("done"), do: "bg-success/15 text-success"
  defp chip_class("failed"), do: "bg-error/20 text-error"
  defp chip_class("rolled-back"), do: "bg-error/15 text-error"
  defp chip_class(_), do: "bg-base-200 opacity-70"

  defp stage_dot("ok"), do: "bg-success"
  defp stage_dot("failed"), do: "bg-error"
  defp stage_dot(_), do: "bg-base-content/30"

  defp stage_status_class("ok"), do: "text-success"
  defp stage_status_class("failed"), do: "text-error"
  defp stage_status_class(_), do: "opacity-60"

  # The FULL detail the stage row carries — a string renders verbatim
  # (build/canary/promote labels incl. the DRY-RUN string), a map/list
  # pretty-prints. Nil/empty → no toggle at all. Never fabricated.
  defp stage_detail_text(entry) do
    case entry["detail"] do
      nil -> nil
      "" -> nil
      text when is_binary(text) -> text
      other -> pretty_json(other)
    end
  end

  # The full status payload the projection row carries, pretty-printed.
  # Only fields that exist on the row — nothing synthesized.
  defp raw_status_json(run) do
    pretty_json(%{
      "temporal_workflow_id" => run.temporal_workflow_id,
      "temporal_run_id" => run.temporal_run_id,
      "status" => run.status,
      "spec_digest" => run.spec_digest,
      "environment_id" => run.environment_id,
      "detail" => run.detail,
      "stage_history" => run.stage_history,
      "started_at" => run.started_at,
      "finished_at" => run.finished_at
    })
  end

  defp pretty_json(term) do
    case Jason.encode(term, pretty: true) do
      {:ok, json} -> json
      _ -> inspect(term, pretty: true)
    end
  end

  defp decision_class("approved"), do: "bg-success/15 text-success"
  defp decision_class(_), do: "bg-error/20 text-error"
end
