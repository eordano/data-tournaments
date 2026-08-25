defmodule TournamentUiWeb.CampaignsLive do
  @moduledoc """
  /campaigns — the campaign layer (journey F-2/F-14).

  Index: one card per campaign (kind, status, objective, window, base
  commit pin, finding counts by state) plus an in-UI "New campaign" form.
  Show (/campaigns/:name): the exploration HUB (contract wave-13 §4) —
  the INDEX.md-shaped ledger (one row per finding: slug, source, state
  chip incl. NO_GO reason, root cause, lens + validation summaries) plus
  every surface the campaign touched: generation domains (judge-queue
  links), fix branches (finding FK), release runs (fix_branch_ship
  linkage first, name-prefix fallback with an honest caption), and the
  bound pipeline. Sections linked only by domain naming convention say
  so in a subtle caption; every section renders gracefully when empty.

  Reads come from TournamentUi.Campaigns (read-only SQLite adapter, 5s
  poll like /runs). The create form NEVER writes the DB from Elixir:
  it shells out to `python3 bin/campaigns.py create-campaign` (ADR 0001 —
  Python owns the schema). `CAMPAIGNS_CLI_CMD` overrides the command for
  tests/deploys.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.CampaignHub
  alias TournamentUi.Campaigns
  alias TournamentUi.Catalog
  alias TournamentUiWeb.DomainNav

  # Read at runtime so tests can point the mutation path at a stub.
  defp cli_cmd, do: System.get_env("CAMPAIGNS_CLI_CMD") || "python3 bin/campaigns.py"

  defp repo_root,
    do: System.get_env("DATA_TOURNAMENTS_REPO") || Path.expand("../../../..", __DIR__)

  @impl true
  def mount(_params, _session, socket) do
    if connected?(socket), do: :timer.send_interval(5_000, :refresh)

    {:ok,
     assign(socket,
       campaigns: [],
       campaign: nil,
       campaign_name: nil,
       hub: nil,
       projects: [],
       form: to_form(new_campaign_params(), as: :campaign),
       create_error: nil
     )}
  end

  @impl true
  def handle_params(params, _uri, socket) do
    {:noreply, socket |> assign(campaign_name: params["name"]) |> load()}
  end

  @impl true
  def handle_info(:refresh, socket) do
    {:noreply, load(socket)}
  end

  @impl true
  def handle_event("validate", %{"campaign" => params}, socket) do
    {:noreply, assign(socket, form: to_form(params, as: :campaign))}
  end

  def handle_event("create", %{"campaign" => params}, socket) do
    name = String.trim(params["name"] || "")
    project = String.trim(params["project"] || "")
    kind = params["kind"] || "bugsweep"

    cond do
      name == "" ->
        {:noreply, assign(socket, create_error: "Campaign name is required.")}

      project == "" ->
        {:noreply, assign(socket, create_error: "Pick a project for the campaign.")}

      true ->
        create_via_cli(socket, name, project, kind, params)
    end
  end

  defp create_via_cli(socket, name, project, kind, params) do
    [cmd | pre_args] = String.split(cli_cmd(), " ", trim: true)

    args =
      pre_args ++
        [
          "create-campaign",
          "--project",
          project,
          "--name",
          name,
          "--kind",
          kind,
          "--objective",
          params["objective"] || "",
          "--time-window",
          params["time_window"] || "",
          "--base-commit",
          params["base_commit"] || ""
        ]

    case System.cmd(cmd, args, stderr_to_stdout: true, cd: repo_root()) do
      {_, 0} ->
        {:noreply,
         socket
         |> assign(create_error: nil, form: to_form(new_campaign_params(), as: :campaign))
         |> put_flash(:info, "Campaign '#{name}' created.")
         |> load()}

      {output, status} ->
        {:noreply,
         assign(socket, create_error: "Create failed (exit #{status}): #{String.trim(output)}")}
    end
  end

  defp new_campaign_params do
    %{
      "name" => "",
      "kind" => "bugsweep",
      "objective" => "",
      "project" => "",
      "time_window" => "",
      "base_commit" => ""
    }
  end

  defp load(%{assigns: %{campaign_name: nil}} = socket) do
    assign(socket,
      campaigns: Campaigns.list_campaigns(),
      campaign: nil,
      projects: Catalog.list_projects()
    )
  end

  defp load(%{assigns: %{campaign_name: name}} = socket) do
    campaign = Campaigns.get_campaign(name)
    hub = if campaign, do: CampaignHub.load(campaign), else: nil
    assign(socket, campaign: campaign, hub: hub)
  end

  @impl true
  def render(%{live_action: :show} = assigns) do
    ~H"""
    <.workspace_page
      current={:campaigns}
      title={@campaign_name}
      subtitle="Campaign ledger: one row per finding."
    >
      <:title_actions>
        <.link navigate="/campaigns" class="btn btn-ghost btn-sm">← All campaigns</.link>
      </:title_actions>

      <%= if @campaign do %>
        <div class="space-y-4">
          <section class="app-card p-5" id="campaign-header">
            <div class="flex items-center gap-3 flex-wrap text-sm">
              <span class="text-[11px] font-medium px-2 py-0.5 rounded-full bg-base-200">
                {@campaign.kind}
              </span>
              <.status_chip status={@campaign.status} />
              <span :if={@campaign.time_window != ""} class="text-xs opacity-55">
                window {@campaign.time_window}
              </span>
              <span
                :if={@campaign.base_commit != ""}
                class="font-mono text-xs opacity-60"
                title="base commit pin"
              >
                @ {String.slice(@campaign.base_commit, 0, 12)}
              </span>
              <span class="text-xs opacity-40 ml-auto">created {@campaign.created_at}</span>
            </div>
            <p :if={@campaign.objective != ""} class="text-sm opacity-70 mt-2">
              {@campaign.objective}
            </p>
          </section>

          <div class="flex items-center gap-2 flex-wrap" id="campaign-cta-row">
            <.link
              :if={@campaign.findings != []}
              navigate="/inspect"
              id="cta-explore-evidence"
              class="btn btn-outline btn-sm"
            >
              Explore evidence
            </.link>
            <.link
              :if={@hub.domains != []}
              navigate={DomainNav.judge_path(List.first(@hub.domains))}
              id="cta-continue-judging"
              class="btn btn-outline btn-sm"
            >
              Continue judging
            </.link>
            <.link
              :if={@hub.branches != []}
              navigate="/branch-fixes"
              id="cta-inspect-branches"
              class="btn btn-outline btn-sm"
            >
              Inspect branches
            </.link>
            <.link
              :if={@hub.releases != []}
              navigate={runs_show_path(List.first(@hub.releases).workflow_id)}
              id="cta-view-release"
              class="btn btn-outline btn-sm"
            >
              View release
            </.link>
          </div>

          <section class="app-card p-5" id="campaign-ledger">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Ledger</h2>
              <span class="text-xs font-mono opacity-40">{length(@campaign.findings)}</span>
            </div>
            <%= if @campaign.findings == [] do %>
              <p class="text-sm opacity-60" id="ledger-empty">
                No findings yet. Findings appear as signals are triaged into
                this campaign.
              </p>
            <% else %>
              <div class="overflow-x-auto">
                <table class="table table-sm w-full">
                  <thead>
                    <tr class="text-xs uppercase tracking-wide opacity-60">
                      <th>Slug</th>
                      <th>Source</th>
                      <th>State</th>
                      <th>Root cause</th>
                      <th>Review</th>
                      <th>Validation</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr :for={f <- @campaign.findings} id={"finding-#{f.slug}"}>
                      <td class="font-mono text-sm font-semibold">{f.slug}</td>
                      <td class="text-xs opacity-70">{f.source_kind}</td>
                      <td><.finding_state_chip state={f.state} no_go_reason={f.no_go_reason} /></td>
                      <td class="text-xs opacity-70 max-w-xs truncate" title={f.root_cause}>
                        {f.root_cause}
                      </td>
                      <td class="text-xs font-mono">{f.lens_summary}</td>
                      <td class="text-xs font-mono">{f.validation_summary}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            <% end %>
          </section>

          <section class="app-card p-5" id="campaign-workorders">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Generated WorkOrders</h2>
              <span class="text-[11px] opacity-40">linked by domain naming</span>
            </div>
            <%= if @hub.domains == [] do %>
              <p class="text-sm opacity-60" id="workorders-empty">
                No generation domain carries this campaign's name yet — WorkOrders
                appear once a domain named after the campaign is created.
              </p>
            <% else %>
              <ul class="space-y-1">
                <li :for={d <- @hub.domains} id={"campaign-domain-#{d}"}>
                  <.link
                    navigate={DomainNav.judge_path(d)}
                    class="font-mono text-sm hover:text-primary transition"
                  >
                    {d}
                  </.link>
                  <span class="text-xs opacity-40 ml-2">→ judge queue</span>
                </li>
              </ul>
            <% end %>
          </section>

          <section class="app-card p-5" id="campaign-branches">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Fix branches</h2>
              <span class="text-xs font-mono opacity-40">{length(@hub.branches)}</span>
            </div>
            <%= if @hub.branches == [] do %>
              <p class="text-sm opacity-60" id="branches-empty">
                No fix branch is registered against this campaign's findings yet.
              </p>
            <% else %>
              <div class="space-y-1.5">
                <div
                  :for={b <- @hub.branches}
                  class="flex items-center gap-3 flex-wrap"
                  id={"campaign-branch-#{b.id}"}
                >
                  <span class={[
                    "text-[11px] font-medium px-2 py-0.5 rounded-full",
                    branch_status_class(b.status)
                  ]}>
                    {b.status}
                  </span>
                  <.link
                    navigate={"/branch-fixes/#{b.id}"}
                    class="font-mono text-sm hover:text-primary transition"
                  >
                    {b.branch_name}
                  </.link>
                  <span class="text-xs font-mono opacity-50">{b.validation_summary}</span>
                </div>
              </div>
            <% end %>
          </section>

          <section class="app-card p-5" id="campaign-releases">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Release runs</h2>
              <span :if={release_source(@hub.releases) == :ship} class="text-[11px] opacity-40">
                linked by ship records
              </span>
              <span :if={release_source(@hub.releases) == :prefix} class="text-[11px] opacity-40">
                linked by workflow naming
              </span>
            </div>
            <%= if @hub.releases == [] do %>
              <p class="text-sm opacity-60" id="releases-empty">
                No release run is linked to this campaign yet — runs appear once a
                branch ships.
              </p>
            <% else %>
              <div class="space-y-1.5">
                <div
                  :for={r <- @hub.releases}
                  class="flex items-center gap-3 flex-wrap"
                  id={"campaign-release-#{release_dom_id(r.workflow_id)}"}
                >
                  <.link
                    navigate={runs_show_path(r.workflow_id)}
                    class="font-mono text-sm hover:text-primary transition"
                  >
                    {r.workflow_id}
                  </.link>
                  <span :if={r.run} class="text-xs font-mono opacity-60">{r.run.status}</span>
                  <span :if={is_nil(r.run)} class="text-xs opacity-40">
                    no run row recorded
                  </span>
                  <.link
                    :if={r.branch_id}
                    navigate={"/branch-fixes/#{r.branch_id}"}
                    class="text-xs opacity-55 hover:text-primary transition"
                  >
                    branch #{r.branch_id}
                  </.link>
                </div>
              </div>
            <% end %>
          </section>

          <section class="app-card p-5" id="campaign-pipeline">
            <div class="flex items-baseline gap-2 mb-3">
              <h2 class="text-xs uppercase tracking-widest opacity-60">Bound pipeline</h2>
              <span class="text-[11px] opacity-40">linked by domain naming</span>
            </div>
            <%= if @hub.bindings == [] do %>
              <p class="text-sm opacity-60" id="pipeline-empty">
                No pipeline binding found for this campaign's domains.
              </p>
            <% else %>
              <div class="space-y-1">
                <div
                  :for={b <- @hub.bindings}
                  class="text-sm font-mono"
                  id={"campaign-binding-#{b.domain}"}
                >
                  {b.domain} → {b.pipeline} v{b.version}
                  <span class="text-xs opacity-50 font-sans ml-1">{b.stage_count} stages</span>
                </div>
              </div>
            <% end %>
          </section>
        </div>
      <% else %>
        <div class="app-card p-8 text-center" id="campaign-missing">
          <div class="text-sm opacity-70">
            No campaign named <strong class="font-mono">{@campaign_name}</strong>.
          </div>
          <.link navigate="/campaigns" class="btn btn-ghost btn-sm mt-4">
            Back to campaigns
          </.link>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:campaigns}
      title="Campaigns"
      subtitle="Bugsweep and release campaigns: a pin, an objective, and a ledger of findings."
    >
      <div class="space-y-6">
        <%= if @campaigns == [] do %>
          <div class="app-card p-8 text-center" id="campaigns-empty">
            <div class="text-sm opacity-70">
              No campaigns yet. Start one below — a campaign bundles a base-commit
              pin, an objective, and a time window; findings accrete into its ledger.
            </div>
          </div>
        <% else %>
          <div class="space-y-2" id="campaigns-list">
            <article
              :for={camp <- @campaigns}
              class="app-card px-5 py-3"
              id={"campaign-#{camp.name}"}
            >
              <div class="flex items-center gap-3 flex-wrap">
                <span class="text-[11px] font-medium px-2 py-0.5 rounded-full bg-base-200">
                  {camp.kind}
                </span>
                <.status_chip status={camp.status} />
                <.link
                  navigate={"/campaigns/#{camp.name}"}
                  class="font-mono text-sm font-semibold hover:text-primary transition"
                >
                  {camp.name}
                </.link>
                <span class="text-xs opacity-55 ml-auto">
                  {camp.finding_count} findings
                  <%= if camp.counts != %{} do %>
                    · {counts_line(camp.counts)}
                  <% end %>
                </span>
              </div>
              <p :if={camp.objective != ""} class="text-xs opacity-60 mt-1">
                {camp.objective}
              </p>
            </article>
          </div>
        <% end %>

        <section class="app-card p-5" id="new-campaign">
          <div class="flex items-baseline gap-2 mb-3">
            <h2 class="text-xs uppercase tracking-widest opacity-60">New campaign</h2>
          </div>

          <div :if={@create_error} class="alert alert-error text-sm mb-3" id="create-error">
            {@create_error}
          </div>

          <%= if @projects == [] do %>
            <p class="text-sm opacity-60" id="no-projects-hint">
              Campaigns belong to a project — register one in the
              <.link navigate="/catalog" class="link">Catalog</.link>
              first.
            </p>
          <% else %>
            <.form
              for={@form}
              id="new-campaign-form"
              phx-change="validate"
              phx-submit="create"
              class="grid gap-3 sm:grid-cols-2"
            >
              <.input field={@form[:name]} type="text" label="Name" placeholder="bugsweep-aug17" />
              <.input
                field={@form[:project]}
                type="select"
                label="Project"
                prompt="Pick a project"
                options={Enum.map(@projects, & &1.name)}
              />
              <.input
                field={@form[:kind]}
                type="select"
                label="Kind"
                options={["bugsweep", "release"]}
              />
              <.input
                field={@form[:objective]}
                type="text"
                label="Objective"
                placeholder="crash-class sweep of the wearable lane"
              />
              <.input
                field={@form[:time_window]}
                type="text"
                label="Window (optional)"
                placeholder="sentry 7d + slack 14d"
              />
              <.input
                field={@form[:base_commit]}
                type="text"
                label="Base commit (optional)"
                placeholder="pin sha"
              />
              <div class="sm:col-span-2">
                <button type="submit" class="btn btn-primary btn-sm" id="create-campaign-button">
                  Create campaign
                </button>
              </div>
            </.form>
          <% end %>
        </section>
      </div>
    </.workspace_page>
    """
  end

  defp counts_line(counts) do
    counts
    |> Enum.sort()
    |> Enum.map_join(" · ", fn {state, n} -> "#{state} #{n}" end)
  end

  # Colon-bearing Temporal workflow ids break path-segment routing on
  # direct GET, so hub links use the query-param detail route (the same
  # convention /runs itself uses).
  defp runs_show_path(workflow_id),
    do: "/runs/show?id=#{URI.encode_www_form(workflow_id)}"

  # All releases in one load share a derivation (ship rows OR the prefix
  # fallback — never mixed), so the section caption comes from the head.
  defp release_source([%{source: source} | _]), do: source
  defp release_source(_), do: nil

  defp release_dom_id(workflow_id), do: String.replace(workflow_id, ~r/[^A-Za-z0-9_-]/, "-")

  defp branch_status_class(status) when status in ~w(validated approved shipped),
    do: "bg-success/15 text-success"

  defp branch_status_class(status) when status in ~w(failed rejected rolled-back),
    do: "bg-error/20 text-error"

  defp branch_status_class("shipping"), do: "bg-info/15 text-info"
  defp branch_status_class(_), do: "bg-base-200 opacity-70"

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

  defp chip_class("active"), do: "bg-info/15 text-info"
  defp chip_class("closed"), do: "bg-base-200 opacity-70"
  defp chip_class(_), do: "bg-base-200 opacity-70"

  attr :state, :string, required: true
  attr :no_go_reason, :string, default: nil

  defp finding_state_chip(assigns) do
    ~H"""
    <span class={[
      "text-[11px] font-medium px-2 py-0.5 rounded-full whitespace-nowrap",
      state_class(@state)
    ]}>
      {@state}
      <%= if @state == "no_go" && @no_go_reason do %>
        · {@no_go_reason}
      <% end %>
    </span>
    """
  end

  defp state_class("confirmed_validated"), do: "bg-success/15 text-success"
  defp state_class("published"), do: "bg-success/15 text-success"
  defp state_class("no_go"), do: "bg-warning/20 text-warning"
  defp state_class("failed_infra"), do: "bg-error/20 text-error"
  defp state_class("executing"), do: "bg-info/15 text-info"
  defp state_class(_), do: "bg-base-200 opacity-70"
end
