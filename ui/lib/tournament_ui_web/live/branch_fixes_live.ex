defmodule TournamentUiWeb.BranchFixesLive do
  @moduledoc """
  /branch-fixes — the end-of-loop surface for automated fix branches.

  Index: one row per fix branch — status chip (stale=warning, failed=error,
  approved/shipped=success), latest RED/GREEN/GUARD validation summary, and
  the latest reviewer decision. Detail (/branch-fixes/:id): branch header
  (repo, base→head shas, patch digest), the FULL validation history (each
  row marked STALE when it tested a sha that is no longer the branch head),
  review history, and the decision panel.

  Decision semantics (client-side affordance ONLY — bin/fix_branches.py
  enforces the same rule for real): a branch is decidable exactly when its
  status is `validated`, the latest validation passed, AND that validation
  tested the current head. Terminal branches (approved/rejected/shipped)
  hide the panel entirely; failed/stale/registered/validating branches get
  an honest status note instead of controls. The reviewing principal is
  DT_OPERATOR; when unset the controls are disabled with a hint (/runs
  precedent). Decisions NEVER write the DB from Elixir: they shell out to
  `python3 bin/fix_branches.py review` (FIX_BRANCHES_CLI_CMD overrides for
  tests/deploys — Campaigns precedent).

  Detail also shows the patch itself (unified diff read from the
  branch-diffs contract path, escaped plain text, capped with an honest
  "truncated" chip) and — for `approved` branches ONLY — a Ship panel that
  starts the release through `python3 bin/branch_ship.py ship`
  (BRANCH_SHIP_CLI_CMD overrides for tests). The gateway is the authority:
  it refuses stale/failed branches and we surface its stderr verbatim.
  Every other status has NO ship control at all — absent, not disabled.
  A `shipping` branch shows the in-flight workflow id (from the
  append-only fix_branch_ship table) instead of the button; a
  `rolled-back` branch shows an honest "requires fresh validation +
  approval" note and NO controls.

  The validation history renders as one scorecard card per run, newest
  first (older cards collapse to a summary line): exact tested sha with a
  CURRENT/STALE chip vs the branch head, RED/GREEN/GUARD as labeled
  pass/fail cells, overall verdict, and the stored log (read from
  DATA_TOURNAMENTS_HOME — branch-logs/<digest>.log first, CAS fallback —
  escaped, 100KB-capped, honest "log not found" when absent). Authoring
  provenance (branch_authoring, when the table/row exists) renders above
  the cards: backend chip (FIXTURE amber / COMMAND blue), workorder ref,
  and a compact provenance JSON summary.
  """
  use TournamentUiWeb, :live_view

  import TournamentUiWeb.DiffComponents

  alias TournamentUi.Diff
  alias TournamentUi.FixBranches

  # Statuses whose decision surface is history/absent, not a panel or note:
  # approved/rejected/shipped are settled; shipping is in flight (its own
  # panel shows the workflow); rolled-back gets its own honest note.
  @terminal ~w(approved rejected shipped shipping rolled-back)

  # Read at runtime so tests can point the mutation path at a stub.
  defp cli_cmd, do: System.get_env("FIX_BRANCHES_CLI_CMD") || "python3 bin/fix_branches.py"

  defp ship_cli_cmd, do: System.get_env("BRANCH_SHIP_CLI_CMD") || "python3 bin/branch_ship.py"

  defp repo_root,
    do: System.get_env("DATA_TOURNAMENTS_REPO") || Path.expand("../../../..", __DIR__)

  defp operator, do: System.get_env("DT_OPERATOR")

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket), do: :timer.send_interval(5_000, :refresh)

    {:ok,
     assign(socket,
       branches: [],
       branch: nil,
       branch_id: nil,
       diff_files: [],
       operator: operator(),
       rationale: "",
       decision_error: nil,
       ship_error: nil,
       toggled_cards: MapSet.new(),
       open_logs: MapSet.new(),
       logs: %{}
     )}
  end

  @impl true
  def handle_params(params, _uri, socket) do
    {:noreply, socket |> assign(branch_id: params["id"]) |> load()}
  end

  @impl true
  def handle_info(:refresh, socket) do
    {:noreply, load(socket)}
  end

  @impl true
  def handle_event("decide", %{"decision" => decision} = params, socket)
      when decision in ~w(approve reject needs-changes) do
    branch = socket.assigns.branch
    rationale = String.trim(params["rationale"] || "")

    case dispatch_review(branch.id, decision, rationale) do
      :ok ->
        {:noreply,
         socket
         |> assign(decision_error: nil, rationale: "")
         |> put_flash(:info, "Review recorded: #{decision}")
         |> load()}

      {:error, output, status} ->
        {:noreply,
         socket
         |> assign(
           decision_error: "Review failed (exit #{status}): #{output}",
           rationale: rationale
         )
         |> load()}
    end
  end

  @impl true
  def handle_event("ship", _params, socket) do
    branch = socket.assigns.branch

    case dispatch_ship(branch.id) do
      {:ok, output} ->
        {:noreply,
         socket
         |> assign(ship_error: nil)
         |> put_flash(:info, "Release started: #{output_tail(output)}")
         |> load()}

      {:error, output, status} ->
        # The gateway refuses stale/failed branches — its message IS the
        # product's honesty, so it goes into the banner verbatim.
        {:noreply,
         socket
         |> assign(ship_error: "Ship refused (exit #{status}): #{output}")
         |> load()}
    end
  end

  @impl true
  def handle_event("toggle-card", %{"id" => id}, socket) do
    {vid, ""} = Integer.parse(id)
    toggled = toggle_member(socket.assigns.toggled_cards, vid)
    {:noreply, assign(socket, toggled_cards: toggled)}
  end

  @impl true
  def handle_event("toggle-log", %{"id" => id, "digest" => digest}, socket) do
    {vid, ""} = Integer.parse(id)
    open_logs = toggle_member(socket.assigns.open_logs, vid)

    # Read lazily, once per open; cached under the validation id so the
    # 5s refresh doesn't re-hit the filesystem for open logs.
    logs =
      if MapSet.member?(open_logs, vid) do
        Map.put_new(socket.assigns.logs, vid, FixBranches.read_log(digest))
      else
        socket.assigns.logs
      end

    {:noreply, assign(socket, open_logs: open_logs, logs: logs)}
  end

  defp toggle_member(set, item) do
    if MapSet.member?(set, item), do: MapSet.delete(set, item), else: MapSet.put(set, item)
  end

  defp dispatch_review(branch_id, decision, rationale) do
    [cmd | pre_args] = String.split(cli_cmd(), " ", trim: true)

    args =
      pre_args ++
        [
          "review",
          "--id",
          to_string(branch_id),
          "--reviewer",
          operator() || "",
          "--decision",
          decision,
          "--rationale",
          rationale
        ]

    case System.cmd(cmd, args, stderr_to_stdout: true, cd: repo_root()) do
      {_, 0} -> :ok
      {output, status} -> {:error, String.trim(output), status}
    end
  end

  defp dispatch_ship(branch_id) do
    [cmd | pre_args] = String.split(ship_cli_cmd(), " ", trim: true)

    args =
      pre_args ++
        ["ship", "--id", to_string(branch_id), "--requested-by", operator() || ""]

    case System.cmd(cmd, args, stderr_to_stdout: true, cd: repo_root()) do
      {output, 0} -> {:ok, String.trim(output)}
      {output, status} -> {:error, String.trim(output), status}
    end
  end

  # Last non-empty line of the gateway's output — enough for a flash, the
  # full transcript belongs to the gateway's own logs.
  defp output_tail(output) do
    output
    |> String.split("\n", trim: true)
    |> List.last()
    |> case do
      nil -> "ok"
      line -> String.trim(line)
    end
  end

  defp load(%{assigns: %{branch_id: nil}} = socket) do
    assign(socket,
      branches: FixBranches.list_branches(),
      branch: nil,
      diff_files: [],
      operator: operator()
    )
  end

  defp load(%{assigns: %{branch_id: id}} = socket) do
    branch = FixBranches.get_branch(id)
    assign(socket, branch: branch, diff_files: parse_diff(branch), operator: operator())
  end

  # Parse once per load, not per render. Malformed/unparseable text yields
  # [] and the template falls back to the raw escaped <pre>.
  defp parse_diff(%{diff: diff}) when is_binary(diff), do: Diff.parse_unified(diff)
  defp parse_diff(_), do: []

  # A branch is decidable only when validated, its latest validation passed,
  # and that validation tested the current head (mirrors bin/fix_branches.py).
  defp decidable?(branch) do
    latest = List.last(branch.validations)

    branch.status == "validated" and latest != nil and latest.passed == 1 and
      branch.current?
  end

  defp terminal?(branch), do: branch.status in @terminal

  # "3 files, +42 −7" — the honest totals over the WHOLE diff file, even
  # when the rendered text is truncated.
  defp changed_files_summary(files) do
    additions = files |> Enum.map(& &1.additions) |> Enum.sum()
    deletions = files |> Enum.map(& &1.deletions) |> Enum.sum()
    noun = if length(files) == 1, do: "file", else: "files"
    "#{length(files)} #{noun}, +#{additions} −#{deletions}"
  end

  defp short(nil), do: "—"
  defp short(sha), do: String.slice(sha, 0, 10)

  @impl true
  def render(%{live_action: :show} = assigns) do
    ~H"""
    <.workspace_page
      current={:branch_fixes}
      title={(@branch && @branch.branch_name) || "Branch ##{@branch_id}"}
      subtitle="Fix branch: validation evidence and review decision."
    >
      <:title_actions>
        <.link navigate="/branch-fixes" class="btn btn-ghost btn-sm">← All branches</.link>
      </:title_actions>

      <%= if @branch do %>
        <div class="space-y-4">
          <section class="app-card p-5" id="branch-header">
            <div class="flex items-center gap-3 flex-wrap text-sm">
              <.status_chip status={@branch.status} />
              <span class="font-mono text-sm font-semibold">{@branch.branch_name}</span>
              <span class="font-mono text-xs opacity-60" title="repository">
                {@branch.repo_path}
              </span>
            </div>
            <div class="flex items-center gap-3 flex-wrap text-xs font-mono opacity-70 mt-2">
              <span title={"base #{@branch.base_sha} → head #{@branch.head_sha}"}>
                {short(@branch.base_sha)} → {short(@branch.head_sha)}
              </span>
              <span :if={@branch.patch_digest} title={"patch digest #{@branch.patch_digest}"}>
                patch {String.slice(@branch.patch_digest, 0, 12)}
              </span>
              <span :if={@branch.workorder_ref} class="opacity-60">
                workorder {@branch.workorder_ref}
              </span>
              <span :if={@branch.finding_id} class="opacity-60">
                finding #{@branch.finding_id}
              </span>
            </div>
          </section>

          <section class="app-card p-5" id="patch-section">
            <div
              class="sticky top-0 z-[2] flex items-baseline gap-2 mb-3 flex-wrap bg-base-100/90 backdrop-blur py-1"
              id="patch-summary-line"
            >
              <h2 class="text-xs uppercase tracking-widest opacity-60">Files changed</h2>
              <span
                :if={@branch.diff}
                class="text-xs font-mono opacity-60"
                id="changed-files-summary"
              >
                {changed_files_summary(@branch.changed_files)}
              </span>
              <span :if={@branch.patch_digest} class="text-xs font-mono opacity-40">
                patch {String.slice(@branch.patch_digest, 0, 12)}
              </span>
              <span
                :if={@branch.diff_truncated?}
                class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-warning/20 text-warning"
                id="diff-truncated-chip"
                title="the diff exceeds the render cap; the file on disk is complete"
              >
                TRUNCATED
              </span>
            </div>

            <div
              :if={@branch.harness_tampered?}
              class="alert alert-error text-sm mb-3"
              id="harness-tampered-banner"
            >
              <span>
                <strong>Trusted harness file changed — validation refused before
                  execution.</strong>
                No candidate code was run; see the latest validation log below for
                the exact protected paths.
              </span>
            </div>

            <%= cond do %>
              <% @branch.diff && @diff_files != [] -> %>
                <.diff_view files={@diff_files} />
              <% @branch.diff -> %>
                <%!-- Parser found no file sections (malformed/exotic diff):
                      honest raw fallback, escaped, never a crash. --%>
                <p class="text-xs opacity-60 mb-2" id="diff-unparsed-note">
                  Could not parse this diff into per-file view — raw patch below.
                </p>
                <pre
                  class="text-xs font-mono whitespace-pre overflow-auto max-h-96 bg-base-200/60 rounded p-3"
                  id="branch-diff"
                >{@branch.diff}</pre>
              <% true -> %>
                <p class="text-sm opacity-60" id="diff-not-captured">
                  Diff not captured — no patch file recorded for this branch.
                </p>
            <% end %>
          </section>

          <section class="app-card p-5" id="validation-history">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Validation scorecards</h2>
              <span class="text-xs font-mono opacity-40">{length(@branch.validations)}</span>
            </div>

            <div
              :if={@branch.authoring}
              class="flex items-center gap-2 flex-wrap text-xs mb-3"
              id="authoring-provenance"
            >
              <span class={[
                "text-[10px] font-semibold px-1.5 py-0.5 rounded",
                backend_chip_class(@branch.authoring.backend)
              ]}>
                {String.upcase(@branch.authoring.backend || "?")}
              </span>
              <span :if={@branch.authoring.workorder_ref} class="font-mono opacity-70">
                workorder {@branch.authoring.workorder_ref}
              </span>
              <span
                :if={@branch.authoring.provenance}
                class="font-mono opacity-50 truncate max-w-xl"
                title={@branch.authoring.provenance}
              >
                {provenance_summary(@branch.authoring.provenance)}
              </span>
            </div>

            <%= if @branch.validations == [] do %>
              <p class="text-sm opacity-60" id="validations-empty">
                No validation runs recorded yet.
              </p>
            <% else %>
              <div class="space-y-2">
                <div
                  :for={{v, idx} <- @branch.validations |> Enum.reverse() |> Enum.with_index()}
                  class="border border-base-200 rounded"
                  id={"validation-#{v.id}"}
                >
                  <%= if expanded?(idx, v.id, @toggled_cards) do %>
                    <div class="p-3 space-y-3" id={"scorecard-#{v.id}"}>
                      <div class="flex items-center gap-2 flex-wrap text-sm">
                        <span class={[
                          "text-[10px] font-semibold px-1.5 py-0.5 rounded",
                          if(v.passed == 1,
                            do: "bg-success/15 text-success",
                            else: "bg-error/20 text-error"
                          )
                        ]}>
                          {if v.passed == 1, do: "PASS", else: "FAIL"}
                        </span>
                        <span class="font-mono text-xs" id={"tested-sha-#{v.id}"}>
                          {v.tested_sha}
                        </span>
                        <%= if v.tested_sha == @branch.head_sha do %>
                          <span
                            class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-success/15 text-success"
                            title="this run tested the branch's current head"
                          >
                            CURRENT
                          </span>
                        <% else %>
                          <span
                            class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-warning/20 text-warning"
                            title="this run tested a sha that is no longer the branch head"
                          >
                            STALE
                          </span>
                        <% end %>
                        <button
                          :if={idx > 0}
                          type="button"
                          phx-click="toggle-card"
                          phx-value-id={v.id}
                          class="btn btn-ghost btn-xs ml-auto"
                        >
                          collapse
                        </button>
                        <span class={["text-[10px] opacity-40", idx == 0 && "ml-auto"]}>
                          {v.created_at}
                        </span>
                      </div>

                      <div class="grid grid-cols-3 gap-2" id={"score-cells-#{v.id}"}>
                        <.score_cell
                          label="RED"
                          got={v.red_observed}
                          want={v.red_intended}
                          vid={v.id}
                        />
                        <.score_cell
                          label="GREEN"
                          got={v.green_passed}
                          want={v.green_total}
                          vid={v.id}
                        />
                        <.score_cell
                          label="GUARD"
                          got={v.guard_passed}
                          want={v.guard_total}
                          vid={v.id}
                        />
                      </div>

                      <div class="text-xs font-mono flex items-center gap-2 flex-wrap">
                        <%= if v.log_digest not in [nil, ""] do %>
                          <span class="opacity-60" title={v.log_digest}>
                            log {String.slice(v.log_digest, 0, 12)}
                          </span>
                          <button
                            type="button"
                            phx-click="toggle-log"
                            phx-value-id={v.id}
                            phx-value-digest={v.log_digest}
                            class="btn btn-ghost btn-xs"
                            id={"log-toggle-#{v.id}"}
                          >
                            {if MapSet.member?(@open_logs, v.id), do: "hide log", else: "view log"}
                          </button>
                        <% else %>
                          <span class="opacity-40" id={"no-log-digest-#{v.id}"}>no log recorded</span>
                        <% end %>
                      </div>

                      <%= if MapSet.member?(@open_logs, v.id) do %>
                        <%= case @logs[v.id] do %>
                          <% {:ok, text, truncated?} -> %>
                            <div>
                              <span
                                :if={truncated?}
                                class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-warning/20 text-warning"
                                id={"log-truncated-#{v.id}"}
                                title="the log exceeds the render cap; the file on disk is complete"
                              >
                                TRUNCATED
                              </span>
                              <pre
                                class="text-xs font-mono whitespace-pre-wrap overflow-auto max-h-96 bg-base-200/60 rounded p-3 mt-1"
                                id={"validation-log-#{v.id}"}
                              >{text}</pre>
                            </div>
                          <% _ -> %>
                            <p class="text-xs opacity-60" id={"log-not-found-#{v.id}"}>
                              Log not found — no file for this digest under
                              <code class="font-mono">branch-logs/</code>
                              or the CAS.
                            </p>
                        <% end %>
                      <% end %>
                    </div>
                  <% else %>
                    <button
                      type="button"
                      phx-click="toggle-card"
                      phx-value-id={v.id}
                      class="w-full flex items-center gap-2 text-sm font-mono flex-wrap p-3 text-left hover:bg-base-200/40 transition"
                      id={"scorecard-summary-#{v.id}"}
                    >
                      <span class={[
                        "text-[10px] font-semibold px-1.5 py-0.5 rounded",
                        if(v.passed == 1,
                          do: "bg-success/15 text-success",
                          else: "bg-error/20 text-error"
                        )
                      ]}>
                        {if v.passed == 1, do: "PASS", else: "FAIL"}
                      </span>
                      <span class="text-xs opacity-60" title={v.tested_sha}>
                        {short(v.tested_sha)}
                      </span>
                      <span class="text-xs">
                        RED {v.red_observed}/{v.red_intended} GREEN {v.green_passed}/{v.green_total} GUARD {v.guard_passed}/{v.guard_total}
                      </span>
                      <span
                        :if={v.tested_sha != @branch.head_sha}
                        class="text-[10px] font-semibold px-1.5 py-0.5 rounded bg-warning/20 text-warning"
                        title="this run tested a sha that is no longer the branch head"
                      >
                        STALE
                      </span>
                      <span class="text-[10px] opacity-40 ml-auto">{v.created_at}</span>
                    </button>
                  <% end %>
                </div>
              </div>
            <% end %>
          </section>

          <section :if={@branch.reviews != []} class="app-card p-5" id="review-history">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Review history</h2>
              <span class="text-xs font-mono opacity-40">{length(@branch.reviews)}</span>
            </div>
            <ul class="space-y-1">
              <li
                :for={r <- @branch.reviews}
                class="flex items-center gap-2 text-sm flex-wrap"
                id={"review-#{r.id}"}
              >
                <span class={[
                  "text-[10px] font-semibold px-1.5 py-0.5 rounded",
                  review_decision_class(r.decision)
                ]}>
                  {r.decision}
                </span>
                <span class="font-mono text-xs">{r.reviewer}</span>
                <span :if={r.rationale not in [nil, ""]} class="text-xs opacity-70">
                  — {r.rationale}
                </span>
                <span class="text-[10px] opacity-40 ml-auto">{r.created_at}</span>
              </li>
            </ul>
          </section>

          <section :if={@branch.status == "approved"} class="app-card p-5" id="ship-panel">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Ship</h2>
            </div>

            <div :if={@ship_error} class="alert alert-error text-sm mb-3" id="ship-error">
              {@ship_error}
            </div>

            <div class="space-y-3">
              <p class="text-sm">
                Release will ship head
                <span class="font-mono font-semibold" id="ship-head-sha">{@branch.head_sha}</span>
                — the exact sha the approval covered. The ship gateway re-checks
                and refuses if the branch went stale or failed since.
              </p>
              <div class="text-xs opacity-70">
                <%= if @operator do %>
                  Shipping as <span class="font-mono font-semibold">{@operator}</span>
                <% else %>
                  No operator identity — set <code class="font-mono">DT_OPERATOR</code>
                  to enable the release.
                <% end %>
              </div>
              <button
                type="button"
                phx-click="ship"
                class="btn btn-primary btn-sm"
                disabled={is_nil(@operator)}
                id="ship-button"
              >
                Start release
              </button>
            </div>
          </section>

          <section :if={@branch.status == "shipping"} class="app-card p-5" id="shipping-panel">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Ship</h2>
            </div>
            <p class="text-sm">
              Release in flight
              <%= if @branch.ship do %>
                — workflow
                <span class="font-mono font-semibold" id="shipping-workflow-id">
                  {@branch.ship.workflow_id}
                </span>
                <span :if={@branch.ship.requested_by} class="text-xs opacity-60">
                  (requested by {@branch.ship.requested_by})
                </span>
              <% else %>
                <span class="text-xs opacity-60" id="shipping-no-record">
                  — no ship record found for this branch yet.
                </span>
              <% end %>
            </p>
          </section>

          <section :if={@branch.status == "rolled-back"} class="app-card p-5" id="rolled-back-note">
            <div class="flex items-baseline gap-2 mb-2">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Rolled back</h2>
            </div>
            <p class="text-sm opacity-70">
              This branch was rolled back — it requires fresh validation + approval
              before it can ship again.
            </p>
          </section>

          <%= cond do %>
            <% terminal?(@branch) -> %>
              <%!-- Terminal branch: the decision is history, no controls. --%>
            <% @branch.status == "validated" -> %>
              <section class="app-card p-5" id="decision-panel">
                <div class="flex items-baseline gap-2 mb-3">
                  <h2 class="text-xs uppercase tracking-widest opacity-60">Decision</h2>
                </div>

                <div :if={@decision_error} class="alert alert-error text-sm mb-3" id="decision-error">
                  {@decision_error}
                </div>

                <form phx-submit="decide" id="decision-form" class="space-y-3">
                  <div class="text-xs opacity-70">
                    <%= if @operator do %>
                      Reviewing as <span class="font-mono font-semibold">{@operator}</span>
                    <% else %>
                      No operator identity — set <code class="font-mono">DT_OPERATOR</code>
                      to enable review decisions.
                    <% end %>
                  </div>
                  <p
                    :if={not decidable?(@branch)}
                    class="text-xs text-warning"
                    id="not-decidable-hint"
                  >
                    Approval requires a passing validation of the current head —
                    the latest run is stale or failing. Re-validate first.
                  </p>
                  <textarea
                    name="rationale"
                    placeholder="Rationale (recorded with the decision)"
                    class="textarea textarea-sm textarea-bordered w-full font-mono text-xs"
                    id="decision-rationale"
                  >{@rationale}</textarea>
                  <div class="flex gap-2">
                    <button
                      type="submit"
                      name="decision"
                      value="approve"
                      class="btn btn-success btn-sm"
                      disabled={is_nil(@operator) or not decidable?(@branch)}
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
                    <button
                      type="submit"
                      name="decision"
                      value="needs-changes"
                      class="btn btn-warning btn-sm"
                      disabled={is_nil(@operator)}
                      id="needs-changes-button"
                    >
                      Needs changes
                    </button>
                  </div>
                </form>
              </section>
            <% true -> %>
              <section class="app-card p-5" id="decision-note">
                <div class="flex items-baseline gap-2 mb-2">
                  <h2 class="text-xs uppercase tracking-widest opacity-60">Decision</h2>
                </div>
                <p class="text-sm opacity-70">{status_note(@branch.status)}</p>
              </section>
          <% end %>
        </div>
      <% else %>
        <div class="app-card p-8 text-center" id="branch-missing">
          <div class="text-sm opacity-70">
            No fix branch with id <strong class="font-mono">{@branch_id}</strong>.
          </div>
          <.link navigate="/branch-fixes" class="btn btn-ghost btn-sm mt-4">
            Back to branches
          </.link>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:branch_fixes}
      title="Branch fixes"
      subtitle="Automated fix branches: validation evidence and review decisions."
    >
      <%= if @branches == [] do %>
        <div class="app-card p-8 text-center" id="branches-empty">
          <div class="text-sm opacity-70">
            No fix branches registered yet. Branches appear when the fix loop
            registers work: <code class="font-mono text-xs">python3 bin/fix_branches.py register</code>.
          </div>
        </div>
      <% else %>
        <div class="overflow-x-auto app-card" id="branches-table">
          <table class="table table-sm w-full">
            <thead>
              <tr class="text-xs uppercase tracking-wide opacity-60">
                <th>Branch</th>
                <th>Status</th>
                <th>Validation</th>
                <th>Review</th>
              </tr>
            </thead>
            <tbody>
              <tr :for={b <- @branches} id={"branch-#{b.id}"}>
                <td>
                  <.link
                    navigate={"/branch-fixes/#{b.id}"}
                    class="font-mono text-sm font-semibold hover:text-primary transition"
                  >
                    {b.branch_name}
                  </.link>
                  <div class="text-[10px] font-mono opacity-50">{b.repo_path}</div>
                </td>
                <td><.status_chip status={b.status} /></td>
                <td class="text-xs font-mono">{b.validation_summary}</td>
                <td class="text-xs font-mono">{b.review_decision || "—"}</td>
              </tr>
            </tbody>
          </table>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  defp status_note("failed"),
    do:
      "This branch's validation FAILED — it cannot be approved. " <>
        "Fix the branch and re-validate; decisions unlock when a passing " <>
        "run tests the current head."

  defp status_note("stale"),
    do:
      "This branch is STALE — its head moved past the last validated sha. " <>
        "Re-validate the current head before any decision."

  defp status_note("registered"),
    do: "This branch is registered but has not been validated yet. No decision until it is."

  defp status_note("validating"),
    do: "Validation is in progress. Decisions unlock when a passing run lands."

  defp status_note(status),
    do: "Status '#{status}' does not accept decisions."

  attr :status, :string, required: true

  defp status_chip(assigns) do
    ~H"""
    <span class={[
      "text-[11px] font-medium px-2 py-0.5 rounded-full whitespace-nowrap",
      chip_class(@status)
    ]}>
      {@status}
    </span>
    """
  end

  defp chip_class("registered"), do: "bg-base-200 opacity-70"
  defp chip_class("validating"), do: "bg-info/15 text-info"
  defp chip_class("validated"), do: "bg-info/15 text-info"
  defp chip_class("failed"), do: "bg-error/20 text-error"
  defp chip_class("stale"), do: "bg-warning/20 text-warning"
  defp chip_class("approved"), do: "bg-success/15 text-success"
  defp chip_class("rejected"), do: "bg-error/20 text-error"
  defp chip_class("shipped"), do: "bg-success/15 text-success"
  defp chip_class("shipping"), do: "bg-info/15 text-info animate-pulse"
  defp chip_class("rolled-back"), do: "bg-error/20 text-error"
  defp chip_class(_), do: "bg-base-200 opacity-70"

  # Authoring backend chips: FIXTURE amber (deterministic test article),
  # COMMAND blue (a real authoring command produced the branch).
  defp backend_chip_class("fixture"), do: "bg-warning/20 text-warning"
  defp backend_chip_class("command"), do: "bg-info/15 text-info"
  defp backend_chip_class(_), do: "bg-base-200 opacity-70"

  # Compact one-line summary of the provenance JSON ("k=v · k=v"); the raw
  # JSON stays available in the title attribute. Non-JSON renders as-is.
  defp provenance_summary(json) when is_binary(json) do
    case Jason.decode(json) do
      {:ok, %{} = map} ->
        map
        |> Enum.sort()
        |> Enum.map_join(" · ", fn {k, v} -> "#{k}=#{compact_value(v)}" end)

      _ ->
        json
    end
  end

  defp provenance_summary(_), do: nil

  defp compact_value(v) when is_binary(v), do: v
  defp compact_value(v) when is_list(v), do: "[#{length(v)} items]"
  defp compact_value(%{} = v), do: "{#{map_size(v)} keys}"
  defp compact_value(v), do: to_string(v)

  # Newest card (idx 0) starts expanded; older cards start collapsed. A
  # click flips whichever default applies.
  defp expanded?(0, id, toggled), do: not MapSet.member?(toggled, id)
  defp expanded?(_idx, id, toggled), do: MapSet.member?(toggled, id)

  attr :label, :string, required: true
  attr :got, :integer, required: true
  attr :want, :integer, required: true
  attr :vid, :integer, required: true

  # One labeled pass/fail cell of the scorecard. A leg passes when it is
  # fully accounted for: got == want AND the denominator is nonzero — the
  # same "full counts" rule bin/branch_validator.py enforces. A 0/0 leg
  # (not exercised) renders neutral, not green.
  defp score_cell(assigns) do
    assigns =
      assign(
        assigns,
        :cell_class,
        cond do
          assigns.want in [nil, 0] -> "bg-base-200/60 opacity-60"
          assigns.got == assigns.want -> "bg-success/10 text-success"
          true -> "bg-error/10 text-error"
        end
      )

    ~H"""
    <div
      class={["rounded p-2 text-center", @cell_class]}
      id={"cell-#{String.downcase(@label)}-#{@vid}"}
    >
      <div class="text-[10px] uppercase tracking-widest opacity-70">{@label}</div>
      <div class="font-mono text-sm font-semibold">{@got}/{@want}</div>
    </div>
    """
  end

  defp review_decision_class("approve"), do: "bg-success/15 text-success"
  defp review_decision_class("needs-changes"), do: "bg-warning/20 text-warning"
  defp review_decision_class(_), do: "bg-error/20 text-error"
end
