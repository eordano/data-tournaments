defmodule TournamentUiWeb.CatalogLive do
  @moduledoc """
  /catalog — legacy index route + live project detail.

  The catalog INDEX moved into the Environment surface (wave-13 §2):
  mounting /catalog push_navigates to /environment?tab=sources so old deep
  links keep working. The DETAIL page (/catalog/:project) stays a live page
  here — it carries the source add/archive forms and snapshot evidence —
  and is linked from the Environment sources tab.

  Writes go through the Python CLI (`bin/catalog.py`) per ADR 0001 — Python
  owns the schema; this LiveView shells out and re-reads. The command is
  overridable via CATALOG_CLI_CMD for tests.

  Source status is derived OFFLINE — no network probes. A source is
  "configured" when its kind is a registered adapter and the locator is
  non-empty; "unknown kind" when the kind isn't in the adapter registry;
  "credential needed: ENV" when the adapter gates on an env var that is
  absent (presence check only — the value is never read into assigns).
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.Catalog

  # Mirrors bin/landscape/adapters/__init__.py adapter_kinds().
  @adapter_kinds ~w(bugsweep_corpus dedup_lists git_local github_api github_autoclosed sentry_csv slack_csv unity_cloud)

  # Kinds whose adapters read a credential from the environment
  # (unity_cloud fetch: config['api_key_env'] default UNITY_CLOUD_BUILD_API_KEY).
  @credential_envs %{"unity_cloud" => "UNITY_CLOUD_BUILD_API_KEY"}

  @tier_options [
    {"1", "Tier 1 — system-captured"},
    {"2", "Tier 2 — team-authored"},
    {"3", "Tier 3 — external-untrusted"}
  ]

  @blank_source %{"name" => "", "kind" => "git_local", "locator" => "", "trust_tier" => "3"}

  @impl true
  def mount(_params, _session, socket) do
    # Legacy index: the catalog landing surface now lives at
    # /environment?tab=sources. Detail (:show) stays live below.
    if socket.assigns.live_action == :index do
      {:ok, push_navigate(socket, to: "/environment?tab=sources")}
    else
      {:ok,
       assign(socket,
         project: nil,
         project_name: nil,
         show_source_form: false,
         source_error: nil,
         source_values: @blank_source
       )}
    end
  end

  @impl true
  def handle_params(params, _uri, socket) do
    case params["project"] do
      nil ->
        {:noreply, socket}

      name ->
        {:noreply, assign(socket, project: Catalog.get_project(name), project_name: name)}
    end
  end

  @impl true
  def handle_event("toggle_source_form", _params, socket) do
    {:noreply,
     assign(socket,
       show_source_form: !socket.assigns.show_source_form,
       source_error: nil
     )}
  end

  def handle_event("create_source", params, socket) do
    name = String.trim(params["name"] || "")
    kind = params["kind"] || ""
    locator = String.trim(params["locator"] || "")
    tier = params["trust_tier"] || "3"
    values = %{"name" => name, "kind" => kind, "locator" => locator, "trust_tier" => tier}

    cond do
      name == "" ->
        {:noreply,
         assign(socket, source_error: "Source name is required.", source_values: values)}

      locator == "" ->
        {:noreply,
         assign(socket,
           source_error: "Locator is required (repo path, owner/name, CSV path, …).",
           source_values: values
         )}

      true ->
        args = [
          "create-source",
          "--project",
          socket.assigns.project_name,
          "--name",
          name,
          "--kind",
          kind,
          "--locator",
          locator,
          "--trust-tier",
          tier
        ]

        case catalog_cli(args) do
          {_out, 0} ->
            {:noreply,
             socket
             |> put_flash(:info, "Source '#{name}' added.")
             |> assign(
               project: Catalog.get_project(socket.assigns.project_name),
               show_source_form: false,
               source_error: nil,
               source_values: @blank_source
             )}

          {out, status} ->
            {:noreply,
             assign(socket,
               source_error: "create-source failed (exit #{status}):\n#{out}",
               source_values: values
             )}
        end
    end
  end

  def handle_event("archive_source", %{"name" => name}, socket) do
    args = ["archive-source", "--project", socket.assigns.project_name, "--name", name]

    case catalog_cli(args) do
      {_out, 0} ->
        {:noreply,
         socket
         |> put_flash(:info, "Source '#{name}' archived.")
         |> assign(project: Catalog.get_project(socket.assigns.project_name), source_error: nil)}

      {out, status} ->
        {:noreply,
         assign(socket, source_error: "archive-source failed (exit #{status}):\n#{out}")}
    end
  end

  # ── CLI shell-out (ADR 0001: Python owns all catalog writes) ──────────

  # CATALOG_CLI_CMD overrides the command for tests/deploys; the default
  # runs the real CLI from the repo root.
  defp catalog_cli(args) do
    [cmd | base] =
      (System.get_env("CATALOG_CLI_CMD") || "python3 bin/catalog.py")
      |> String.split(" ", trim: true)

    System.cmd(cmd, base ++ args, stderr_to_stdout: true, cd: repo_root())
  end

  defp repo_root,
    do: System.get_env("DATA_TOURNAMENTS_REPO") || Path.expand("../../../..", __DIR__)

  # ── Offline source status (F-9, honest version — no fake probes) ──────

  defp source_status(%{kind: kind, locator: locator}) do
    credential_env = @credential_envs[kind]

    cond do
      kind not in @adapter_kinds ->
        {:unknown_kind, "unknown kind"}

      credential_env != nil and not env_present?(credential_env) ->
        {:credential, "credential needed: #{credential_env}"}

      locator in [nil, ""] ->
        {:incomplete, "locator missing"}

      true ->
        {:configured, "configured"}
    end
  end

  # Presence only — the value is never rendered or assigned.
  defp env_present?(name) do
    case System.get_env(name) do
      nil -> false
      "" -> false
      _ -> true
    end
  end

  defp status_class(:configured), do: "bg-success/15 text-success"
  defp status_class(:credential), do: "bg-warning/15 text-warning"
  defp status_class(:unknown_kind), do: "bg-error/20 text-error"
  defp status_class(:incomplete), do: "bg-warning/15 text-warning"

  defp status_title(:unknown_kind),
    do: "not a registered adapter kind; known kinds: " <> Enum.join(@adapter_kinds, ", ")

  defp status_title(:credential),
    do: "the adapter reads this env var at fetch time; set it in the server environment"

  defp status_title(:configured), do: "kind registered and locator set (offline check)"
  defp status_title(:incomplete), do: "source has no locator"

  @impl true
  def render(%{live_action: :show} = assigns) do
    ~H"""
    <.workspace_page
      current={:environment}
      flash={@flash}
      title={@project_name}
      subtitle="Project landscape: components, evidence sources, and captured snapshots."
    >
      <:title_actions>
        <.link navigate="/environment?tab=sources" class="btn btn-ghost btn-sm">
          ← All projects
        </.link>
      </:title_actions>

      <%= if @project do %>
        <div class="space-y-4">
          <section class="app-card p-5" id="catalog-components">
            <.section_header label="Components" count={length(@project.components)} />
            <%= if @project.components == [] do %>
              <p class="text-sm opacity-60">No components registered.</p>
            <% else %>
              <ul class="space-y-1.5">
                <li :for={c <- @project.components} class="flex items-center gap-2 text-sm">
                  <span class="font-mono font-medium">{c.name}</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-base-200 opacity-70">
                    {c.kind}
                  </span>
                </li>
              </ul>
            <% end %>
          </section>

          <section class="app-card p-5" id="catalog-sources">
            <div class="flex items-baseline justify-between mb-3">
              <.section_header label="Sources" count={length(@project.sources)} />
              <button
                type="button"
                id="add-source-btn"
                phx-click="toggle_source_form"
                class="btn btn-ghost btn-xs"
              >
                <%= if @show_source_form do %>
                  Cancel
                <% else %>
                  + Add source
                <% end %>
              </button>
            </div>

            <.source_form
              :if={@show_source_form}
              values={@source_values}
              error={@source_error}
            />
            <div
              :if={@source_error && !@show_source_form}
              id="source-error"
              class="alert alert-error text-sm whitespace-pre-wrap mb-3"
            >
              {@source_error}
            </div>

            <%= if @project.sources == [] do %>
              <p class="text-sm opacity-60">No sources registered.</p>
            <% else %>
              <ul class="space-y-1.5">
                <li
                  :for={s <- @project.sources}
                  class="flex items-center gap-2 text-sm min-w-0"
                  id={"source-row-#{s.name}"}
                >
                  <span class="font-mono font-medium">{s.name}</span>
                  <span class="text-[10px] px-1.5 py-0.5 rounded bg-base-200 opacity-70">
                    {s.kind}
                  </span>
                  <span class={[
                    "text-[10px] font-semibold px-1.5 py-0.5 rounded",
                    tier_class(s.trust_tier)
                  ]}>
                    {tier_label(s.trust_tier)}
                  </span>
                  <.source_status_chip source={s} />
                  <span class="text-[10px] opacity-55 shrink-0">
                    {s.evidence_count} evidence
                  </span>
                  <span class="font-mono text-[10px] opacity-40 truncate" title={s.locator}>
                    {s.locator}
                  </span>
                  <button
                    type="button"
                    id={"archive-source-#{s.name}"}
                    phx-click="archive_source"
                    phx-value-name={s.name}
                    data-confirm={"Archive source '#{s.name}'? Its evidence history is kept; it is excluded from future snapshots."}
                    class="btn btn-ghost btn-xs opacity-60 hover:opacity-100 ml-auto shrink-0"
                  >
                    Archive
                  </button>
                </li>
              </ul>
            <% end %>
          </section>

          <section class="app-card p-5" id="catalog-snapshots">
            <.section_header label="Recent snapshots" count={length(@project.snapshots)} />
            <%= if @project.snapshots == [] do %>
              <p class="text-sm opacity-60">No snapshots captured yet.</p>
            <% else %>
              <ul class="space-y-2">
                <li :for={snap <- @project.snapshots} class="text-sm">
                  <div class="flex items-center gap-2 flex-wrap">
                    <span class="font-mono font-medium" title={snap.digest}>
                      {digest_prefix(snap.digest)}
                    </span>
                    <span class="text-xs opacity-60">
                      {snap.evidence_count} evidence refs
                    </span>
                    <span class="text-xs opacity-40">{format_date(snap.created_at)}</span>
                  </div>
                  <div :if={snap.packs != []} class="flex items-center gap-1.5 mt-1 flex-wrap">
                    <span
                      :for={pack <- snap.packs}
                      class="text-[10px] font-mono px-1.5 py-0.5 rounded bg-base-200 opacity-70"
                      title={pack.digest}
                    >
                      {pack.role} · {digest_prefix(pack.digest)}
                    </span>
                  </div>
                </li>
              </ul>
            <% end %>
          </section>
        </div>
      <% else %>
        <div class="app-card p-8 text-center">
          <div class="text-sm opacity-70">
            No project named <strong class="font-mono">{@project_name}</strong> in the catalog.
          </div>
          <.link navigate="/environment?tab=sources" class="btn btn-ghost btn-sm mt-4">
            Back to catalog
          </.link>
        </div>
      <% end %>
    </.workspace_page>
    """
  end

  # Legacy index route (:index) — mount already push_navigated to
  # /environment?tab=sources; render nothing meaningful in the interim.
  def render(assigns) do
    ~H"""
    <div class="p-8 text-sm opacity-60">
      Catalog moved — <.link navigate="/environment?tab=sources" class="link">Environment → Sources</.link>.
    </div>
    """
  end

  # (project_form/1 moved to EnvironmentLive's sources tab — wave-13.)

  attr :values, :map, required: true
  attr :error, :any, required: true

  defp source_form(assigns) do
    assigns = assign(assigns, kinds: @adapter_kinds, tiers: @tier_options)

    ~H"""
    <form
      phx-submit="create_source"
      id="add-source-form"
      class="space-y-3 mb-4 p-4 rounded bg-base-200/40"
    >
      <div class="grid sm:grid-cols-2 gap-3">
        <label class="block">
          <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Name</div>
          <input
            name="name"
            id="new-source-name"
            value={@values["name"]}
            placeholder="e.g. repo"
            class="input input-bordered input-sm w-full font-mono"
          />
        </label>
        <label class="block">
          <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Kind</div>
          <select
            name="kind"
            id="new-source-kind"
            class="select select-bordered select-sm w-full font-mono"
          >
            <option :for={k <- @kinds} value={k} selected={@values["kind"] == k}>{k}</option>
          </select>
        </label>
        <label class="block">
          <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Locator</div>
          <input
            name="locator"
            id="new-source-locator"
            value={@values["locator"]}
            placeholder="repo path, owner/name, CSV path, org/project…"
            class="input input-bordered input-sm w-full font-mono"
          />
        </label>
        <label class="block">
          <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Trust tier</div>
          <select
            name="trust_tier"
            id="new-source-tier"
            class="select select-bordered select-sm w-full"
          >
            <option :for={{v, label} <- @tiers} value={v} selected={@values["trust_tier"] == v}>
              {label}
            </option>
          </select>
        </label>
      </div>

      <div :if={@error} id="source-form-error" class="alert alert-error text-sm whitespace-pre-wrap">
        {@error}
      </div>

      <div class="flex justify-end">
        <button type="submit" class="btn btn-primary btn-sm" id="create-source-btn">
          Add source
        </button>
      </div>
    </form>
    """
  end

  attr :source, :map, required: true

  defp source_status_chip(assigns) do
    {level, text} = source_status(assigns.source)
    assigns = assign(assigns, level: level, text: text)

    ~H"""
    <span
      class={["text-[10px] font-semibold px-1.5 py-0.5 rounded shrink-0", status_class(@level)]}
      title={status_title(@level)}
      data-status={@text}
    >
      {@text}
    </span>
    """
  end

  attr :label, :string, required: true
  attr :count, :integer, required: true

  defp section_header(assigns) do
    ~H"""
    <div class="flex items-baseline gap-2 mb-3">
      <h2 class="text-xs uppercase tracking-widest opacity-60">{@label}</h2>
      <span class="text-xs font-mono opacity-40">{@count}</span>
    </div>
    """
  end

  defp digest_prefix(digest) when is_binary(digest), do: String.slice(digest, 0, 12)

  defp tier_class(3), do: "bg-error/20 text-error"
  defp tier_class(2), do: "bg-warning/15 text-warning"
  defp tier_class(_), do: "bg-success/15 text-success"

  defp tier_label(1), do: "TIER1 · system"
  defp tier_label(2), do: "TIER2 · internal"
  defp tier_label(3), do: "TIER3 · UNTRUSTED"
  defp tier_label(other), do: "TIER#{other}"

  defp format_date(nil), do: "—"
  defp format_date(value) when is_binary(value), do: String.slice(value, 0, 10)
end
