defmodule TournamentUiWeb.DomainEditLive do
  @moduledoc """
  /domains/:name/edit — edit description, corpus_source, generator prompt,
  and judge prompt for an existing domain.

  Stage-1-and-stage-2 are merged on a single page (no AI drafting needed —
  the user already has prompts; they're just adjusting). Save shells out
  to bin/domain_builder_cli.py --edit which pushes new prompt versions
  (idempotent if unchanged) and updates the domain row.
  """
  use TournamentUiWeb, :live_view

  alias TournamentUi.Domains
  alias TournamentUi.LangfusePrompts

  @builder_script System.get_env("DOMAIN_BUILDER_SCRIPT") ||
                    "bin/domain_builder_cli.py"
  @repo_root System.get_env("DATA_TOURNAMENTS_REPO") ||
               Path.expand("../../../../..", __ENV__.file)

  @impl true
  def mount(%{"name" => name}, _session, socket) do
    case Domains.get(name) do
      nil ->
        {:ok,
         socket
         |> put_flash(:error, "Domain '#{name}' not found.")
         |> push_navigate(to: "/domains")}

      spec ->
        gen_text =
          case LangfusePrompts.get(spec.generator_prompt, "production") do
            text when is_binary(text) -> text
            _ -> ""
          end

        jud_text =
          case LangfusePrompts.get(spec.judge_prompt, "production") do
            text when is_binary(text) -> text
            _ -> ""
          end

        {kind, fields} = corpus_to_form(spec.corpus_source)

        {:ok,
         assign(socket,
           spec: spec,
           description: spec.description,
           corpus_kind: kind,
           corpus_path: fields[:path] || "",
           corpus_query: fields[:query] || "",
           corpus_glob: fields[:glob] || "*.md",
           corpus_inline: fields[:inline] || "",
           generator_prompt: gen_text,
           judge_prompt: jud_text,
           prompt_backend: LangfusePrompts.backend_info(),
           save_error: nil,
           saving: false
         )}
    end
  end

  @impl true
  def handle_event("change", params, socket) do
    {:noreply,
     assign(socket,
       description: params["description"] || socket.assigns.description,
       corpus_kind: params["corpus_kind"] || socket.assigns.corpus_kind,
       corpus_path: params["corpus_path"] || socket.assigns.corpus_path,
       corpus_query: params["corpus_query"] || socket.assigns.corpus_query,
       corpus_glob: params["corpus_glob"] || socket.assigns.corpus_glob,
       corpus_inline: params["corpus_inline"] || socket.assigns.corpus_inline,
       generator_prompt: params["generator_prompt"] || socket.assigns.generator_prompt,
       judge_prompt: params["judge_prompt"] || socket.assigns.judge_prompt
     )}
  end

  def handle_event("save", _params, socket) do
    spec_json = build_corpus_spec(socket.assigns)

    args = [
      @builder_script,
      "--edit",
      "--name",
      socket.assigns.spec.name,
      "--description",
      socket.assigns.description,
      "--generator-prompt",
      socket.assigns.generator_prompt,
      "--judge-prompt",
      socket.assigns.judge_prompt,
      "--corpus-spec",
      Jason.encode!(spec_json)
    ]

    case System.cmd("python3", args, stderr_to_stdout: true, cd: @repo_root) do
      {_out, 0} ->
        {:noreply,
         socket
         |> put_flash(:info, "Domain '#{socket.assigns.spec.name}' updated.")
         |> push_navigate(to: "/domains")}

      {output, status} ->
        {:noreply, assign(socket, save_error: "Save failed (exit #{status}):\n#{output}")}
    end
  end

  defp corpus_to_form(%{"kind" => "inline", "items" => items}) do
    inline_text =
      items
      |> Enum.map(fn it ->
        Map.get(it, "text") || Map.get(it, "body") || Jason.encode!(it)
      end)
      |> Enum.join("\n")

    {"inline", %{inline: inline_text}}
  end

  defp corpus_to_form(%{"kind" => "filesystem"} = m),
    do: {"filesystem", %{path: m["root"] || "", glob: m["glob"] || "*.md"}}

  defp corpus_to_form(%{"kind" => "sqlite"} = m),
    do: {"sqlite", %{path: m["path"] || "", query: m["query"] || ""}}

  defp corpus_to_form(_), do: {"inline", %{inline: ""}}

  # Preserve an explicitly stored artifact choice; default new/legacy
  # specs to rich WorkOrders (user: legacy cards are "too small").
  defp build_corpus_spec(a) do
    base =
      case a.corpus_kind do
        "inline" ->
          items =
            (a.corpus_inline || "")
            |> String.split("\n", trim: true)
            |> Enum.map(&%{"text" => &1})

          %{"kind" => "inline", "items" => items}

        "filesystem" ->
          %{"kind" => "filesystem", "root" => a.corpus_path, "glob" => a.corpus_glob}

        "sqlite" ->
          %{"kind" => "sqlite", "path" => a.corpus_path, "query" => a.corpus_query}
      end

    existing = a.spec.corpus_source["artifact"]
    Map.put(base, "artifact", existing || "work-order")
  end

  @impl true
  def render(assigns) do
    ~H"""
    <.workspace_page
      current={:domains}
      flash={@flash}
      max_width="max-w-3xl"
      title={"Edit " <> @spec.name}
      subtitle={"Configure source and evaluation lens. Prompt edits create a version in #{@prompt_backend.label}."}
    >
      <:title_actions>
        <.link navigate="/domains" id="edit-back-btn" class="btn btn-ghost btn-sm">
          ← Back to domains
        </.link>
      </:title_actions>

      <form phx-change="change" phx-submit="save" id="edit-form" class="space-y-5">
        <label class="app-card p-5 block">
          <div class="text-sm font-medium mb-1">Description</div>
          <input name="description" value={@description} class="input input-bordered w-full" />
        </label>

        <div class="app-card p-5">
          <div class="text-sm font-medium mb-3">Corpus source</div>
          <div class="flex gap-2 mb-4">
            <%= for kind <- ~w(inline filesystem sqlite) do %>
              <label class={[
                "btn btn-sm",
                @corpus_kind == kind && "btn-primary",
                @corpus_kind != kind && "btn-ghost border app-hairline"
              ]}>
                <input
                  type="radio"
                  name="corpus_kind"
                  value={kind}
                  checked={@corpus_kind == kind}
                  class="hidden"
                />
                {kind}
              </label>
            <% end %>
          </div>

          <%= case @corpus_kind do %>
            <% "inline" -> %>
              <label class="block">
                <div class="text-xs uppercase tracking-wider opacity-70 mb-1">
                  Items (one per line)
                </div>
                <textarea
                  name="corpus_inline"
                  class="textarea textarea-bordered w-full h-32 font-mono text-xs"
                  placeholder="paste sample items, one per line"
                ><%= @corpus_inline %></textarea>
              </label>
            <% "filesystem" -> %>
              <div class="grid grid-cols-2 gap-3">
                <label class="block">
                  <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Root path</div>
                  <input
                    name="corpus_path"
                    value={@corpus_path}
                    class="input input-bordered input-sm w-full font-mono"
                  />
                </label>
                <label class="block">
                  <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Glob</div>
                  <input
                    name="corpus_glob"
                    value={@corpus_glob}
                    class="input input-bordered input-sm w-full font-mono"
                  />
                </label>
              </div>
            <% "sqlite" -> %>
              <div class="space-y-3">
                <label class="block">
                  <div class="text-xs uppercase tracking-wider opacity-70 mb-1">DB path</div>
                  <input
                    name="corpus_path"
                    value={@corpus_path}
                    class="input input-bordered input-sm w-full font-mono"
                  />
                </label>
                <label class="block">
                  <div class="text-xs uppercase tracking-wider opacity-70 mb-1">Query</div>
                  <textarea
                    name="corpus_query"
                    class="textarea textarea-bordered w-full h-24 font-mono text-xs"
                  ><%= @corpus_query %></textarea>
                </label>
              </div>
          <% end %>
        </div>

        <label class="app-card p-5 block">
          <div class="text-sm font-medium mb-1">Generator prompt</div>
          <div class="text-xs opacity-60 mb-2 font-mono">
            {@spec.generator_prompt} · {@prompt_backend.label}
          </div>
          <textarea
            name="generator_prompt"
            class="textarea textarea-bordered prompt-editor w-full h-48 font-mono text-xs"
          ><%= @generator_prompt %></textarea>
        </label>

        <label class="app-card p-5 block">
          <div class="text-sm font-medium mb-1">Judge prompt</div>
          <div class="text-xs opacity-60 mb-2 font-mono">
            {@spec.judge_prompt} · {@prompt_backend.label}
          </div>
          <textarea
            name="judge_prompt"
            class="textarea textarea-bordered prompt-editor w-full h-48 font-mono text-xs"
          ><%= @judge_prompt %></textarea>
        </label>

        <%= if @save_error do %>
          <div class="alert alert-error text-sm whitespace-pre-wrap">{@save_error}</div>
        <% end %>

        <div class="flex justify-between">
          <.link navigate="/domains" class="btn btn-ghost">Cancel</.link>
          <button type="submit" class="btn btn-primary">Save changes</button>
        </div>
      </form>
    </.workspace_page>
    """
  end
end
