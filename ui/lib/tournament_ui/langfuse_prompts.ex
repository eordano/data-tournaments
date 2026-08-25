defmodule TournamentUi.LangfusePrompts do
  @moduledoc """
  Unified prompt store used by the UI.

  `PROMPT_BACKEND=local` reads the same `prompts.json` file as the Python
  runtime. `PROMPT_BACKEND=langfuse` uses the Langfuse API. `auto` selects
  Langfuse only when both credentials exist. This keeps the editor, runtime,
  and optimizer on one explicit source of truth.
  """

  defmodule PromptInfo do
    @moduledoc false
    defstruct [
      :name,
      all_versions: [],
      labels: MapSet.new(),
      production_version: nil,
      candidate_version: nil
    ]
  end

  @doc "Active prompt backend (`:local` or `:langfuse`)."
  def backend do
    configured =
      Application.get_env(:tournament_ui, :prompt_backend) ||
        System.get_env("PROMPT_BACKEND") ||
        "auto"

    case configured |> to_string() |> String.downcase() |> String.trim() do
      "local" -> :local
      "langfuse" -> :langfuse
      _ -> if credentials_present?(), do: :langfuse, else: :local
    end
  end

  @doc "Human-readable backend metadata for page copy and diagnostics."
  def backend_info do
    case backend() do
      :local -> %{backend: :local, label: "Local prompt store", location: local_store_path()}
      :langfuse -> %{backend: :langfuse, label: "Langfuse", location: host()}
    end
  end

  @doc "All prompts grouped by name. Returns [] on any read error."
  def list do
    case backend() do
      :local -> list_local()
      :langfuse -> list_langfuse()
    end
  rescue
    _ -> []
  end

  @doc "Fetch a prompt body by name and label. Returns the prompt text or `:error`."
  def get(name, label \\ "production") do
    case backend() do
      :local -> get_local(name, label)
      :langfuse -> get_langfuse(name, label)
    end
  rescue
    _ -> :error
  end

  defp get_langfuse(name, label) do
    case do_get("/api/public/v2/prompts/#{URI.encode(name)}", label: label) do
      {:ok, %{status: 200, body: %{"prompt" => text}}} -> text
      _ -> :error
    end
  end

  @doc "Move the `production` label to the given version. Returns :ok or {:error, _}."
  def promote(name, version) when is_integer(version) do
    set_label(name, version, "production")
  end

  def set_label(name, version, label) when is_integer(version) and is_binary(label) do
    case backend() do
      :local -> set_local_label(name, version, label)
      :langfuse -> set_langfuse_label(name, version, label)
    end
  rescue
    e -> {:error, e}
  end

  defp set_langfuse_label(name, version, label) do
    body = %{"newLabels" => [label]}
    path = "/api/public/v2/prompts/#{URI.encode(name)}/versions/#{version}"

    case req(method: :patch, path: path, json: body) do
      {:ok, %{status: s}} when s in 200..299 -> :ok
      {:ok, resp} -> {:error, {:bad_status, resp.status}}
      {:error, e} -> {:error, e}
    end
  end

  # ── Local JSON store ─────────────────────────────────────────────────

  defp list_local do
    local_store()
    |> Map.get("prompts", %{})
    |> Enum.map(fn {name, versions} ->
      Enum.reduce(versions, %PromptInfo{name: name}, fn row, info ->
        version = row["version"]
        labels = row["labels"] || []

        %{
          info
          | all_versions: [version | info.all_versions],
            labels: MapSet.union(info.labels, MapSet.new(labels)),
            production_version:
              if("production" in labels, do: version, else: info.production_version),
            candidate_version:
              if("candidate" in labels, do: version, else: info.candidate_version)
        }
      end)
      |> then(&%{&1 | all_versions: Enum.sort(&1.all_versions)})
    end)
    |> Enum.sort_by(& &1.name)
  end

  defp get_local(name, label) do
    local_store()
    |> Map.get("prompts", %{})
    |> Map.get(name, [])
    |> Enum.reverse()
    |> Enum.find(&(label in (&1["labels"] || [])))
    |> case do
      %{"prompt" => text} when is_binary(text) -> text
      _ -> :error
    end
  end

  defp set_local_label(name, version, label) do
    data = local_store()
    prompts = Map.get(data, "prompts", %{})
    versions = Map.get(prompts, name, [])

    if Enum.any?(versions, &(&1["version"] == version)) do
      updated =
        Enum.map(versions, fn row ->
          labels = Enum.reject(row["labels"] || [], &(&1 == label))
          labels = if row["version"] == version, do: Enum.uniq([label | labels]), else: labels
          Map.put(row, "labels", labels)
        end)

      write_local_store(Map.put(data, "prompts", Map.put(prompts, name, updated)))
      :ok
    else
      {:error, :version_not_found}
    end
  end

  defp local_store do
    case File.read(local_store_path()) do
      {:ok, json} ->
        case Jason.decode(json) do
          {:ok, %{"prompts" => prompts} = data} when is_map(prompts) -> data
          _ -> %{"prompts" => %{}}
        end

      {:error, :enoent} ->
        %{"prompts" => %{}}

      {:error, reason} ->
        raise File.Error, reason: reason, action: "read", path: local_store_path()
    end
  end

  defp write_local_store(data) do
    path = local_store_path()
    File.mkdir_p!(Path.dirname(path))
    temp = path <> ".tmp"
    File.write!(temp, Jason.encode_to_iodata!(data, pretty: true))
    File.rename!(temp, path)
  end

  defp local_store_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "prompts.json")
  end

  # ── Langfuse reads ───────────────────────────────────────────────────

  defp list_langfuse do
    case do_get("/api/public/v2/prompts", page: 1, limit: 100) do
      {:ok, %{status: 200, body: %{"data" => data}}} ->
        data |> aggregate_versions() |> Map.values() |> Enum.sort_by(& &1.name)

      _ ->
        []
    end
  end

  # ── HTTP plumbing ──────────────────────────────────────────────────────

  defp do_get(path, params) do
    req(method: :get, path: path, params: params)
  end

  defp req(opts) do
    method = Keyword.fetch!(opts, :method)
    path = Keyword.fetch!(opts, :path)

    base_opts = [
      method: method,
      url: host() <> path,
      auth: {:basic, "#{public_key()}:#{secret_key()}"},
      receive_timeout: 5_000
    ]

    base_opts
    |> Keyword.merge(Keyword.take(opts, [:params, :json]))
    |> maybe_test_plug()
    |> Req.request()
  end

  # Only stub HTTP in test env. In dev/prod we hit the real Langfuse.
  if Mix.env() == :test do
    defp maybe_test_plug(opts), do: Keyword.put(opts, :plug, {Req.Test, __MODULE__})
  else
    defp maybe_test_plug(opts), do: opts
  end

  defp host do
    Application.get_env(:tournament_ui, :langfuse_host) ||
      System.get_env("LANGFUSE_HOST") ||
      System.get_env("LANGFUSE_BASE_URL") ||
      "https://cloud.langfuse.com"
  end

  defp public_key do
    Application.get_env(:tournament_ui, :langfuse_public_key) ||
      System.get_env("LANGFUSE_PUBLIC_KEY") || ""
  end

  defp secret_key do
    Application.get_env(:tournament_ui, :langfuse_secret_key) ||
      System.get_env("LANGFUSE_SECRET_KEY") || ""
  end

  defp credentials_present? do
    public_key() not in [nil, ""] and secret_key() not in [nil, ""]
  end

  defp aggregate_versions(rows) do
    Enum.reduce(rows, %{}, fn row, acc ->
      name = row["name"]
      version = row["version"]
      labels = row["labels"] || []

      info = Map.get(acc, name, %PromptInfo{name: name})

      info = %{
        info
        | all_versions: [version | info.all_versions],
          labels: MapSet.union(info.labels, MapSet.new(labels)),
          production_version:
            if("production" in labels, do: version, else: info.production_version),
          candidate_version: if("candidate" in labels, do: version, else: info.candidate_version)
      }

      Map.put(acc, name, info)
    end)
    |> Enum.into(%{}, fn {k, v} ->
      {k, %{v | all_versions: Enum.sort(v.all_versions)}}
    end)
  end
end
