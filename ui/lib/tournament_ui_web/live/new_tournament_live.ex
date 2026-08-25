defmodule TournamentUiWeb.NewTournamentLive do
  use TournamentUiWeb, :live_view

  alias TournamentUi.{Builder, PromptPresets, Browser, Runner, InputSources}

  @impl true
  def mount(_params, _session, socket) do
    presets = PromptPresets.all()
    preset = List.first(presets)
    start_dir = Browser.default_root()

    listing =
      case Browser.list(start_dir) do
        {:ok, l} -> l
        _ -> nil
      end

    {:ok,
     socket
     |> assign(
       presets: presets,
       preset_id: preset.id,
       name: "",
       parallelism: 1,
       seed: 20_260_101,
       server_paths: "",
       match_prompt: preset.match_prompt,
       required_sections: Enum.join(preset.required_sections, "\n"),
       preview_state: :idle,
       preview_output: nil,
       preview_error: nil,
       create_error: nil,
       roots: Browser.roots(),
       browse_dir: start_dir,
       listing: listing,
       browse_error: nil,
       selected: MapSet.new(),
       # DB source
       db_url: "",
       db_query: "SELECT id AS name, body AS content FROM articles LIMIT 100",
       db_preview_rows: [],
       db_preview_error: nil,
       # JS source
       js_code: default_js(),
       js_source_name: nil,
       js_source_content: nil,
       js_preview_rows: [],
       js_preview_error: nil
     )
     |> allow_upload(:inputs,
       accept: :any,
       max_entries: 200,
       max_file_size: 2_000_000
     )
     |> allow_upload(:js_source,
       accept: :any,
       max_entries: 1,
       max_file_size: 5_000_000
     )}
  end

  defp default_js do
    """
    // Split `input.content` into sub-documents. Return [{name, content}, ...].
    // Example: split a large text file into one chunk per ~1000 chars.
    function process(input) {
      const size = 1000;
      const out = [];
      for (let i = 0, n = 1; i < input.content.length; i += size, n++) {
        out.push({
          name: `${input.name}-${n}.txt`,
          content: input.content.slice(i, i + size)
        });
      }
      return out;
    }
    """
  end

  @impl true
  def handle_event("change", %{"_target" => ["preset_id"], "preset_id" => id} = _params, socket) do
    preset = PromptPresets.get(id)

    {:noreply,
     assign(socket,
       preset_id: id,
       match_prompt: preset.match_prompt,
       required_sections: Enum.join(preset.required_sections, "\n")
     )}
  end

  def handle_event("change", params, socket) do
    {:noreply,
     assign(socket,
       name: params["name"] || socket.assigns.name,
       parallelism: to_int(params["parallelism"], socket.assigns.parallelism),
       seed: to_int(params["seed"], socket.assigns.seed),
       server_paths: params["server_paths"] || socket.assigns.server_paths,
       match_prompt: params["match_prompt"] || socket.assigns.match_prompt,
       required_sections: params["required_sections"] || socket.assigns.required_sections,
       db_url: params["db_url"] || socket.assigns.db_url,
       db_query: params["db_query"] || socket.assigns.db_query,
       js_code: params["js_code"] || socket.assigns.js_code
     )}
  end

  def handle_event("validate_upload", _params, socket), do: {:noreply, socket}

  def handle_event("cancel_upload", %{"ref" => ref}, socket) do
    {:noreply, cancel_upload(socket, :inputs, ref)}
  end

  def handle_event("cd", %{"path" => path}, socket) do
    case Browser.list(path) do
      {:ok, l} -> {:noreply, assign(socket, listing: l, browse_dir: path, browse_error: nil)}
      {:error, reason} -> {:noreply, assign(socket, browse_error: reason)}
    end
  end

  def handle_event("toggle_file", %{"path" => path}, socket) do
    sel =
      if MapSet.member?(socket.assigns.selected, path) do
        MapSet.delete(socket.assigns.selected, path)
      else
        MapSet.put(socket.assigns.selected, path)
      end

    {:noreply, assign(socket, selected: sel)}
  end

  def handle_event("select_all_here", _params, socket) do
    case socket.assigns.listing do
      %{files: files} ->
        sel = Enum.reduce(files, socket.assigns.selected, &MapSet.put(&2, &1.path))
        {:noreply, assign(socket, selected: sel)}

      _ ->
        {:noreply, socket}
    end
  end

  def handle_event("select_none_here", _params, socket) do
    case socket.assigns.listing do
      %{files: files} ->
        paths = MapSet.new(files, & &1.path)
        sel = MapSet.difference(socket.assigns.selected, paths)
        {:noreply, assign(socket, selected: sel)}

      _ ->
        {:noreply, socket}
    end
  end

  def handle_event("select_recursive", %{"path" => path}, socket) do
    case Browser.all_files_recursive(path) do
      {:ok, paths} ->
        sel = Enum.reduce(paths, socket.assigns.selected, &MapSet.put(&2, &1))
        {:noreply, assign(socket, selected: sel, browse_error: nil)}

      {:error, reason} ->
        {:noreply, assign(socket, browse_error: reason)}
    end
  end

  def handle_event("deselect_recursive", %{"path" => path}, socket) do
    case Browser.all_files_recursive(path) do
      {:ok, paths} ->
        set = MapSet.new(paths)
        sel = MapSet.difference(socket.assigns.selected, set)
        {:noreply, assign(socket, selected: sel)}

      {:error, reason} ->
        {:noreply, assign(socket, browse_error: reason)}
    end
  end

  def handle_event("remove_selected", %{"path" => path}, socket) do
    {:noreply, assign(socket, selected: MapSet.delete(socket.assigns.selected, path))}
  end

  def handle_event("clear_selected", _params, socket) do
    {:noreply, assign(socket, selected: MapSet.new())}
  end

  # ── DB source ────────────────────────────────────────────────────────

  def handle_event("db_preview", _params, socket) do
    case InputSources.db_preview(socket.assigns.db_url, socket.assigns.db_query) do
      {:ok, rows} ->
        {:noreply, assign(socket, db_preview_rows: rows, db_preview_error: nil)}

      {:error, reason} ->
        {:noreply, assign(socket, db_preview_rows: [], db_preview_error: reason)}
    end
  end

  def handle_event("db_materialise", _params, socket) do
    name =
      socket.assigns.name
      |> case do
        nil -> ""
        s -> s
      end

    if name == "" do
      {:noreply,
       assign(socket, db_preview_error: "set a tournament name first (used for upload dir)")}
    else
      case InputSources.db_materialise(socket.assigns.db_url, socket.assigns.db_query, name) do
        {:ok, paths} ->
          sel = Enum.reduce(paths, socket.assigns.selected, &MapSet.put(&2, &1))

          {:noreply,
           assign(socket, selected: sel, db_preview_error: nil)
           |> put_flash(:info, "added #{length(paths)} rows from DB to inputs")}

        {:error, reason} ->
          {:noreply, assign(socket, db_preview_error: reason)}
      end
    end
  end

  # ── JS source ────────────────────────────────────────────────────────

  def handle_event("js_consume_upload", _params, socket) do
    consumed =
      consume_uploaded_entries(socket, :js_source, fn %{path: path}, entry ->
        content = File.read!(path)
        {:ok, %{name: entry.client_name, content: content}}
      end)

    case consumed do
      [%{name: n, content: c}] ->
        {:noreply,
         assign(socket,
           js_source_name: n,
           js_source_content: c,
           js_preview_error: nil
         )
         |> put_flash(:info, "loaded #{n} (#{byte_size(c)} bytes)")}

      [] ->
        {:noreply, assign(socket, js_preview_error: "no file to load")}

      _ ->
        {:noreply, assign(socket, js_preview_error: "choose exactly one file")}
    end
  end

  def handle_event("js_preview", _params, socket) do
    cond do
      socket.assigns.js_source_content == nil ->
        {:noreply, assign(socket, js_preview_error: "upload a source file first")}

      true ->
        case InputSources.js_preview(
               socket.assigns.js_source_name,
               socket.assigns.js_source_content,
               socket.assigns.js_code
             ) do
          {:ok, rows} ->
            {:noreply, assign(socket, js_preview_rows: rows, js_preview_error: nil)}

          {:error, reason} ->
            {:noreply, assign(socket, js_preview_rows: [], js_preview_error: reason)}
        end
    end
  end

  def handle_event("js_materialise", _params, socket) do
    name =
      socket.assigns.name
      |> case do
        nil -> ""
        s -> s
      end

    cond do
      name == "" ->
        {:noreply, assign(socket, js_preview_error: "set a tournament name first")}

      socket.assigns.js_source_content == nil ->
        {:noreply, assign(socket, js_preview_error: "upload a source file first")}

      true ->
        case InputSources.js_materialise(
               socket.assigns.js_source_name,
               socket.assigns.js_source_content,
               socket.assigns.js_code,
               name
             ) do
          {:ok, paths} ->
            sel = Enum.reduce(paths, socket.assigns.selected, &MapSet.put(&2, &1))

            {:noreply,
             assign(socket, selected: sel, js_preview_error: nil)
             |> put_flash(:info, "added #{length(paths)} rows from JS transform to inputs")}

          {:error, reason} ->
            {:noreply, assign(socket, js_preview_error: reason)}
        end
    end
  end

  def handle_event("preview", _params, socket) do
    with {:ok, inputs} <- collected_inputs(socket),
         [a, b | _] <- inputs do
      parent = self()
      prompt = socket.assigns.match_prompt
      req = parse_sections(socket.assigns.required_sections)
      par = socket.assigns.parallelism

      Task.start(fn ->
        result = Builder.preview_match(prompt, req, a, b, parallelism: par)
        send(parent, {:preview_done, result})
      end)

      {:noreply, assign(socket, preview_state: :running, preview_output: nil, preview_error: nil)}
    else
      {:error, reason} ->
        {:noreply, assign(socket, preview_error: reason)}

      _ ->
        {:noreply,
         assign(socket,
           preview_error: "need at least two inputs (uploads + server paths combined)."
         )}
    end
  end

  def handle_event("create", _params, socket) do
    with true <- socket.assigns.name != "" || {:error, "name required"},
         {:ok, inputs} <- collected_inputs(socket),
         true <- length(inputs) >= 2 || {:error, "need at least 2 inputs"} do
      params = [
        name: socket.assigns.name,
        parallelism: socket.assigns.parallelism,
        seed: socket.assigns.seed,
        inputs: inputs,
        required_sections: parse_sections(socket.assigns.required_sections),
        match_prompt: socket.assigns.match_prompt
      ]

      case Builder.write_config(params) do
        {:ok, path, slug} ->
          Runner.start(path)

          {:noreply,
           socket
           |> put_flash(
             :info,
             "created #{slug} and started run — tail /tmp/harness/runs/#{slug}.log"
           )
           |> push_navigate(to: "/?select=#{slug}")}
      end
    else
      {:error, reason} -> {:noreply, assign(socket, create_error: reason)}
      false -> {:noreply, assign(socket, create_error: "name and at least 2 inputs required")}
    end
  end

  @impl true
  def handle_info({:preview_done, {:ok, md, _err}}, socket) do
    {:noreply, assign(socket, preview_state: :done, preview_output: md)}
  end

  def handle_info({:preview_done, {:error, reason}}, socket) do
    {:noreply, assign(socket, preview_state: :error, preview_error: reason)}
  end

  # ── helpers ──────────────────────────────────────────────────────────

  defp collected_inputs(socket) do
    saved =
      case socket.assigns.name do
        "" ->
          []

        _ ->
          dir = Builder.upload_dir(socket.assigns.name)
          File.mkdir_p!(dir)

          consume_uploaded_entries(socket, :inputs, fn %{path: path}, entry ->
            dest = Path.join(dir, entry.client_name)
            File.cp!(path, dest)
            {:ok, dest}
          end)
      end

    server =
      socket.assigns.server_paths
      |> String.split(~r/[\r\n]+/, trim: true)
      |> Enum.map(&String.trim/1)
      |> Enum.reject(&(&1 == ""))
      |> Enum.flat_map(&expand_glob/1)
      |> Enum.uniq()

    browsed = MapSet.to_list(socket.assigns.selected)
    combined = Enum.uniq(browsed ++ server ++ saved)
    missing = Enum.reject(combined, &File.exists?/1)

    cond do
      missing != [] -> {:error, "paths not found: #{Enum.join(missing, ", ")}"}
      combined == [] -> {:error, "no inputs"}
      true -> {:ok, combined}
    end
  end

  defp expand_glob(path) do
    if String.contains?(path, "*") do
      Path.wildcard(path)
    else
      [path]
    end
  end

  defp parse_sections(text) do
    text
    |> String.split("\n", trim: true)
    |> Enum.map(&String.trim/1)
    |> Enum.reject(&(&1 == ""))
  end

  defp to_int(nil, default), do: default

  defp to_int(s, default) do
    case Integer.parse(s) do
      {n, _} -> n
      :error -> default
    end
  end

  defp format_size(nil), do: ""
  defp format_size(n) when n < 1024, do: "#{n} B"
  defp format_size(n) when n < 1_048_576, do: "#{Float.round(n / 1024, 1)} KB"
  defp format_size(n), do: "#{Float.round(n / 1_048_576, 1)} MB"

  defp crumbs_roots(nil), do: []

  defp crumbs_roots(%{breadcrumbs: [first | _]}), do: [first.path]

  defp crumbs_roots(_), do: []

  # ── render ───────────────────────────────────────────────────────────

  @impl true
  def render(assigns) do
    ~H"""
    <div class="min-h-screen workspace">
      <div class="max-w-5xl mx-auto px-6 py-8">
        <div class="flex items-center justify-between mb-6 pb-4 border-b app-hairline">
          <.workspace_nav current={:brackets} />
          <.link navigate="/brackets" class="text-sm opacity-60 hover:opacity-100">
            ← Direct brackets
          </.link>
        </div>
        <div class="flex items-start justify-between mb-6">
          <div>
            <h1 class="text-2xl font-semibold tracking-tight">Direct artifact bracket</h1>
            <p class="text-sm opacity-60 mt-1">
              Compare prepared artifacts that can all answer the same match question.
            </p>
          </div>
          <.link navigate="/domains" class="btn btn-ghost btn-sm gap-1">
            Use domain workflow instead <span aria-hidden="true">→</span>
          </.link>
        </div>

        <div class="alert mb-5 items-start border border-info/25 bg-info/10 text-sm">
          <.icon name="hero-information-circle" class="size-5 shrink-0 mt-0.5" />
          <div>
            <div class="font-semibold">Keep one comparison question per bracket</div>
            <p class="opacity-75 mt-0.5">
              Every input should be comparable under the match prompt below. To extract findings
              from code across correctness, security, or another category, create a separate
              <.link navigate="/start" class="underline font-medium">domain workflow</.link>
              for each category.
            </p>
          </div>
        </div>

        <form phx-change="change" phx-submit="create" id="new-tournament" class="space-y-5">
          <div class="app-card p-6">
            <div class="grid grid-cols-3 gap-4">
              <div>
                <label class="label pb-1"><span class="label-text font-semibold">Name</span></label>
                <input
                  type="text"
                  name="name"
                  value={@name}
                  class="input input-bordered w-full"
                  placeholder="e.g. algo-shootout"
                />
              </div>
              <div>
                <label class="label pb-1">
                  <span class="label-text font-semibold">Parallelism</span>
                </label>
                <select name="parallelism" class="select select-bordered w-full">
                  <%= for n <- [1, 2, 4] do %>
                    <option value={n} selected={@parallelism == n}>{n}</option>
                  <% end %>
                </select>
              </div>
              <div>
                <label class="label pb-1">
                  <span class="label-text font-semibold">Random seed</span>
                </label>
                <input type="number" name="seed" value={@seed} class="input input-bordered w-full" />
              </div>
            </div>
          </div>

          <div class="app-card p-6">
            <div class="mb-4">
              <h2 class="font-semibold text-base">Inputs</h2>
              <p class="text-xs opacity-60 mt-0.5">
                Pick files from disk, paste paths, upload, or generate from a DB / JS transform.
              </p>
            </div>

            <div class="grid grid-cols-3 gap-4">
              <!-- Column 1: server-side browser -->
              <div class="col-span-2">
                <label class="label pb-1">
                  <span class="label-text">Server-side file browser</span>
                </label>
                <div class="border app-hairline rounded-lg overflow-hidden">
                  <div class="flex items-center gap-1 bg-base-200 px-2 py-1.5 text-xs flex-wrap">
                    <%= for root <- @roots do %>
                      <button
                        type="button"
                        phx-click="cd"
                        phx-value-path={root}
                        class={"btn btn-xs " <> if(root == List.first(crumbs_roots(@listing)), do: "btn-primary", else: "btn-ghost")}
                      >
                        {root}
                      </button>
                    <% end %>
                  </div>

                  <%= if @listing do %>
                    <div class="flex items-center gap-1 px-2 py-1 bg-base-100 text-xs overflow-x-auto">
                      <%= for {crumb, idx} <- Enum.with_index(@listing.breadcrumbs) do %>
                        <%= if idx > 0 do %>
                          <span class="opacity-40">/</span>
                        <% end %>
                        <button
                          type="button"
                          phx-click="cd"
                          phx-value-path={crumb.path}
                          class="btn btn-xs btn-ghost"
                        >
                          {crumb.name}
                        </button>
                      <% end %>
                      <%= if @listing.parent do %>
                        <button
                          type="button"
                          phx-click="cd"
                          phx-value-path={@listing.parent}
                          class="btn btn-xs btn-ghost ml-auto"
                        >
                          ↑ parent
                        </button>
                      <% end %>
                    </div>

                    <div class="flex items-center gap-2 px-2 py-1.5 border-t app-hairline text-xs">
                      <span class="opacity-60">here:</span>
                      <button type="button" phx-click="select_all_here" class="btn btn-xs">
                        all
                      </button>
                      <button type="button" phx-click="select_none_here" class="btn btn-xs">
                        none
                      </button>
                      <span class="opacity-60">+ subfolders:</span>
                      <button
                        type="button"
                        phx-click="select_recursive"
                        phx-value-path={@listing.dir}
                        class="btn btn-xs"
                      >
                        all ⇩
                      </button>
                      <button
                        type="button"
                        phx-click="deselect_recursive"
                        phx-value-path={@listing.dir}
                        class="btn btn-xs"
                      >
                        none ⇩
                      </button>
                      <span class="opacity-60 ml-auto">
                        {length(@listing.dirs)} dirs · {length(@listing.files)} files
                      </span>
                    </div>

                    <ul class="max-h-80 overflow-y-auto divide-y app-hairline text-sm">
                      <%= for d <- @listing.dirs do %>
                        <li class="flex items-center px-2 py-1.5 hover:bg-base-200">
                          <button
                            type="button"
                            class="flex items-center gap-2 flex-1 text-left cursor-pointer"
                            phx-click="cd"
                            phx-value-path={d.path}
                          >
                            <span class="w-5">📁</span>
                            <span class="truncate flex-1">{d.name}</span>
                          </button>
                          <button
                            type="button"
                            class="btn btn-xs btn-ghost"
                            phx-click="select_recursive"
                            phx-value-path={d.path}
                            title="select all files recursively in this folder"
                          >
                            ⊕
                          </button>
                        </li>
                      <% end %>
                      <%= for f <- @listing.files do %>
                        <li class="flex items-center px-2 py-1.5 hover:bg-base-200">
                          <label class="flex items-center gap-2 flex-1 cursor-pointer">
                            <input
                              type="checkbox"
                              class="checkbox checkbox-xs"
                              checked={MapSet.member?(@selected, f.path)}
                              phx-click="toggle_file"
                              phx-value-path={f.path}
                            />
                            <span class="w-5">📄</span>
                            <span class="truncate flex-1">{f.name}</span>
                            <span class="opacity-50 text-xs font-mono">{format_size(f.size)}</span>
                          </label>
                        </li>
                      <% end %>
                      <%= if @listing.files == [] and @listing.dirs == [] do %>
                        <li class="px-2 py-3 text-center opacity-50 text-xs">(empty)</li>
                      <% end %>
                    </ul>
                  <% end %>

                  <%= if @browse_error do %>
                    <div class="alert alert-warning text-xs py-1 px-2 rounded-none">
                      {@browse_error}
                    </div>
                  <% end %>
                </div>

                <div class="mt-2 flex items-center justify-between text-xs">
                  <span class="opacity-70">
                    selected: <b class="font-mono">{MapSet.size(@selected)}</b> file(s)
                  </span>
                  <%= if MapSet.size(@selected) > 0 do %>
                    <button type="button" phx-click="clear_selected" class="btn btn-xs btn-ghost">
                      clear all
                    </button>
                  <% end %>
                </div>
                <%= if MapSet.size(@selected) > 0 do %>
                  <div class="mt-1 border app-hairline rounded max-h-32 overflow-y-auto text-xs">
                    <%= for p <- Enum.sort(MapSet.to_list(@selected)) do %>
                      <div class="flex items-center px-2 py-0.5 hover:bg-base-200">
                        <span class="truncate flex-1 font-mono">{p}</span>
                        <button
                          type="button"
                          phx-click="remove_selected"
                          phx-value-path={p}
                          class="btn btn-xs btn-ghost"
                        >
                          ✕
                        </button>
                      </div>
                    <% end %>
                  </div>
                <% end %>
              </div>
              <div class="space-y-3">
                <div>
                  <label class="label pb-1">
                    <span class="label-text text-xs">
                      Extra server paths (one per line; globs ok)
                    </span>
                  </label>
                  <textarea
                    name="server_paths"
                    class="textarea textarea-bordered w-full font-mono text-xs h-24"
                    placeholder="/path/to/file1.ts\n/path/to/dir/*.ts"
                  >{@server_paths}</textarea>
                </div>

                <div>
                  <label class="label pb-1">
                    <span class="label-text text-xs">Or drag-and-drop upload</span>
                  </label>
                  <label
                    phx-drop-target={@uploads.inputs.ref}
                    class="border-2 border-dashed app-hairline rounded-lg p-3 text-center text-xs opacity-80 flex flex-col justify-center cursor-pointer hover:bg-base-200 transition"
                  >
                    <.live_file_input upload={@uploads.inputs} class="hidden" />
                    <span class="underline">click to browse</span>
                    <div class="text-xs opacity-60 mt-1">or drop (max 200, 2 MB each)</div>
                  </label>
                  <%= for entry <- @uploads.inputs.entries do %>
                    <div class="flex items-center gap-2 text-xs mt-1">
                      <span class="truncate flex-1">{entry.client_name}</span>
                      <progress
                        class="progress progress-primary w-20"
                        value={entry.progress}
                        max="100"
                      >
                      </progress>
                      <button
                        type="button"
                        phx-click="cancel_upload"
                        phx-value-ref={entry.ref}
                        class="btn btn-xs"
                      >
                        ✕
                      </button>
                    </div>
                  <% end %>
                </div>
              </div>
            </div>
          </div>

          <details class="app-card overflow-hidden">
            <summary class="px-6 py-4 font-semibold cursor-pointer flex items-center gap-2 hover:bg-base-200/50 transition">
              <span class="text-lg">📦</span>
              <span>Source: database query</span>
              <span class="text-xs font-normal opacity-60 ml-auto">
                returns <code>name, content</code>
              </span>
            </summary>
            <div class="px-6 pb-6 pt-2 space-y-3 border-t app-hairline">
              <p class="text-xs opacity-70">
                Paste a DB URL (sqlite: or postgres://). Your SELECT must project <code>name</code>
                and <code>content</code>.
                Preview shows the first 5 rows; "Add to inputs" writes up to 1000 rows into the uploads dir and selects them.
              </p>
              <input
                type="text"
                name="db_url"
                value={@db_url}
                class="input input-bordered w-full font-mono text-xs"
                placeholder="sqlite:***@host/db"
              />
              <textarea
                name="db_query"
                class="textarea textarea-bordered w-full font-mono text-xs h-28"
              >{@db_query}</textarea>
              <div class="flex gap-2">
                <button type="button" phx-click="db_preview" class="btn btn-sm">Preview 5</button>
                <button type="button" phx-click="db_materialise" class="btn btn-sm btn-primary">
                  Add all to inputs
                </button>
              </div>
              <%= if @db_preview_error do %>
                <div class="alert alert-error text-xs whitespace-pre-wrap">{@db_preview_error}</div>
              <% end %>
              <%= if @db_preview_rows != [] do %>
                <div class="text-xs border app-hairline rounded max-h-48 overflow-y-auto">
                  <%= for row <- @db_preview_rows do %>
                    <div class="px-2 py-1 border-b app-hairline">
                      <div class="font-mono font-semibold">{row.name}</div>
                      <div class="opacity-70 truncate">{String.slice(row.content, 0, 200)}</div>
                    </div>
                  <% end %>
                </div>
              <% end %>
            </div>
          </details>

          <details class="app-card overflow-hidden">
            <summary class="px-6 py-4 font-semibold cursor-pointer flex items-center gap-2 hover:bg-base-200/50 transition">
              <span class="text-lg">⚙️</span>
              <span>Source: one file + JS transform → many inputs</span>
              <span class="text-xs font-normal opacity-60 ml-auto">
                requires <code>node</code> on PATH
              </span>
            </summary>
            <div class="px-6 pb-6 pt-2 space-y-3 border-t app-hairline">
              <p class="text-xs opacity-70">
                Upload one file, then write a JS function <code>process(input)</code>
                that returns an array
                of <code>{"{name, content}"}</code>
                objects. Each becomes one tournament input.
              </p>
              <div>
                <label
                  phx-drop-target={@uploads.js_source.ref}
                  class="border-2 border-dashed app-hairline rounded p-3 text-center text-xs opacity-80 flex flex-col justify-center cursor-pointer hover:bg-base-200 transition"
                >
                  <.live_file_input upload={@uploads.js_source} class="hidden" />
                  <span class="underline">upload source file</span>
                  <%= if @js_source_name do %>
                    <div class="opacity-60 mt-1">
                      loaded: <span class="font-mono">{@js_source_name}</span>
                      ({byte_size(@js_source_content || "")} bytes)
                    </div>
                  <% else %>
                    <div class="opacity-60 mt-1">click or drop (max 5 MB)</div>
                  <% end %>
                </label>
                <%= for entry <- @uploads.js_source.entries do %>
                  <div class="flex items-center gap-2 text-xs mt-1">
                    <span class="truncate flex-1">{entry.client_name}</span>
                    <progress class="progress progress-primary w-20" value={entry.progress} max="100">
                    </progress>
                  </div>
                <% end %>
                <%= if @uploads.js_source.entries != [] do %>
                  <button type="button" phx-click="js_consume_upload" class="btn btn-xs mt-1">
                    load selected file
                  </button>
                <% end %>
              </div>
              <textarea
                name="js_code"
                class="textarea textarea-bordered w-full font-mono text-xs h-56"
              >{@js_code}</textarea>
              <div class="flex gap-2">
                <button type="button" phx-click="js_preview" class="btn btn-sm">Preview 5</button>
                <button type="button" phx-click="js_materialise" class="btn btn-sm btn-primary">
                  Add all to inputs
                </button>
              </div>
              <%= if @js_preview_error do %>
                <div class="alert alert-error text-xs whitespace-pre-wrap">{@js_preview_error}</div>
              <% end %>
              <%= if @js_preview_rows != [] do %>
                <div class="text-xs border app-hairline rounded max-h-48 overflow-y-auto">
                  <%= for row <- @js_preview_rows do %>
                    <div class="px-2 py-1 border-b app-hairline">
                      <div class="font-mono font-semibold">{row.name}</div>
                      <div class="opacity-70 truncate">{String.slice(row.content, 0, 200)}</div>
                    </div>
                  <% end %>
                </div>
              <% end %>
            </div>
          </details>

          <div class="app-card p-6">
            <div class="mb-4">
              <h2 class="font-semibold text-base">Match prompt</h2>
              <p class="text-xs opacity-60 mt-0.5">
                Template seen by the agent on each match. Placeholders: <code class="text-xs">{"{LABEL}"}</code>, <code class="text-xs">{"{INPUTS}"}</code>, <code class="text-xs">{"{N_INPUTS}"}</code>.
              </p>
            </div>

            <label class="label pb-1"><span class="label-text">Preset</span></label>
            <select name="preset_id" class="select select-bordered w-full mb-4">
              <%= for p <- @presets do %>
                <option value={p.id} selected={p.id == @preset_id}>{p.name} — {p.description}</option>
              <% end %>
            </select>

            <label class="label pb-1"><span class="label-text">Prompt text</span></label>
            <textarea name="match_prompt" class="textarea textarea-bordered prompt-editor w-full h-72">{@match_prompt}</textarea>

            <label class="label pb-1 mt-3">
              <span class="label-text">Required section headers (one per line)</span>
            </label>
            <textarea
              name="required_sections"
              class="textarea textarea-bordered prompt-editor w-full h-32"
            >{@required_sections}</textarea>
          </div>

          <div class="app-card p-6">
            <div class="flex items-center justify-between mb-3">
              <div>
                <h2 class="font-semibold text-base">Preview</h2>
                <p class="text-xs opacity-60 mt-0.5">
                  Run a single match against the first two selected inputs.
                </p>
              </div>
              <button
                type="button"
                phx-click="preview"
                class="btn btn-sm btn-outline"
                disabled={@preview_state == :running}
              >
                <%= if @preview_state == :running do %>
                  <span class="loading loading-spinner loading-xs"></span> Running…
                <% else %>
                  Run preview
                <% end %>
              </button>
            </div>
            <%= if @preview_error do %>
              <div class="alert alert-error text-xs whitespace-pre-wrap">{@preview_error}</div>
            <% end %>
            <%= if @preview_output do %>
              <pre class="text-xs bg-base-200 p-3 rounded whitespace-pre-wrap max-h-96 overflow-auto font-mono">{@preview_output}</pre>
            <% end %>
            <%= if !@preview_error and !@preview_output and @preview_state != :running do %>
              <div class="text-xs opacity-60 border border-dashed app-hairline rounded p-4 text-center">
                <div class="font-medium opacity-80">No preview yet</div>
                <div class="opacity-70 mt-1">
                  Click "Run preview" once you've selected at least 2 inputs.
                </div>
              </div>
            <% end %>
          </div>

          <%= if @create_error do %>
            <div class="alert alert-error text-sm">{@create_error}</div>
          <% end %>

          <div class="form-footer flex gap-3 justify-end items-center -mx-6">
            <span class="text-xs opacity-60 mr-auto pl-6">
              <%= if @name == "" do %>
                Set a name to create the direct bracket.
              <% else %>
                Ready to create: <span class="font-mono">{@name}</span>
              <% end %>
            </span>
            <.link navigate="/brackets" class="btn btn-ghost">Cancel</.link>
            <button type="submit" class="btn btn-primary" disabled={@name == ""}>
              Create direct bracket
            </button>
          </div>
        </form>
      </div>
    </div>
    """
  end
end
