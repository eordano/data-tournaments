defmodule TournamentUi.InputSources do
  @moduledoc """
  Materialise "virtual inputs" (from a DB query or a JS transform) to real
  files on disk so the harness can pass them as paths.

  Both sources return `[%{name: String.t, content: String.t}, ...]`; we write
  each entry to `$uploads/<slug>/<safe_name>` and return the absolute paths.
  """

  alias TournamentUi.Builder

  @preview_cap 5
  @max_entries 1000
  @max_content_bytes 1_000_000

  # ── DB source ────────────────────────────────────────────────────────

  @doc """
  Runs a `SELECT name, content FROM …` query against a DB and returns a
  preview list. `db_url` shapes we understand:
    - `sqlite:///path/to/db.sqlite`
    - `postgres://user:pass@host:port/db`

  For postgres we require `psql` on PATH; for sqlite we require `sqlite3`.
  """
  def db_preview(db_url, query) do
    with {:ok, rows} <- db_fetch(db_url, query, @preview_cap) do
      {:ok, normalise(rows)}
    end
  end

  def db_materialise(db_url, query, tournament_name) do
    with {:ok, rows} <- db_fetch(db_url, query, @max_entries) do
      materialise(rows, tournament_name)
    end
  end

  defp db_fetch(db_url, query, limit) do
    cond do
      String.starts_with?(db_url, "sqlite:") ->
        sqlite_fetch(String.replace_prefix(db_url, "sqlite://", ""), query, limit)

      String.starts_with?(db_url, "postgres") ->
        psql_fetch(db_url, query, limit)

      true ->
        {:error, "unknown scheme: #{db_url}"}
    end
  end

  defp sqlite_fetch(path, query, limit) do
    # Use the Exqlite driver already on deps.
    try do
      {:ok, conn} = Exqlite.Sqlite3.open(path, mode: :readonly)

      try do
        # Wrap query so we always take ≤limit rows.
        wrapped = "SELECT name, content FROM (#{query}) LIMIT #{limit}"
        {:ok, stmt} = Exqlite.Sqlite3.prepare(conn, wrapped)
        rows = collect_sqlite(conn, stmt, [])
        Exqlite.Sqlite3.release(conn, stmt)
        {:ok, Enum.reverse(rows)}
      after
        Exqlite.Sqlite3.close(conn)
      end
    rescue
      e -> {:error, Exception.message(e)}
    end
  end

  defp collect_sqlite(conn, stmt, acc) do
    case Exqlite.Sqlite3.step(conn, stmt) do
      {:row, [name, content]} -> collect_sqlite(conn, stmt, [{name, content} | acc])
      :done -> acc
      other -> raise "sqlite step: #{inspect(other)}"
    end
  end

  defp psql_fetch(url, query, limit) do
    # We wrap with row_to_json so psql emits compact JSON lines we can parse.
    sql = """
    SELECT json_build_object('name', name, 'content', content)::text
    FROM (#{query}) _sub
    LIMIT #{limit};
    """

    case System.cmd("psql", [url, "-Atq", "-c", sql], stderr_to_stdout: true) do
      {out, 0} ->
        rows =
          out
          |> String.split("\n", trim: true)
          |> Enum.map(fn line ->
            case Jason.decode(line) do
              {:ok, %{"name" => n, "content" => c}} -> {n, c}
              _ -> nil
            end
          end)
          |> Enum.reject(&is_nil/1)

        {:ok, rows}

      {out, code} ->
        {:error, "psql exit #{code}: #{String.slice(out, 0, 1000)}"}
    end
  end

  # ── JS source ────────────────────────────────────────────────────────

  @doc """
  Runs a user-supplied JS transform on the given input content. The user's
  code must define a function called `process(input)` that returns an array
  of `{name, content}` objects. `input` is an object `{name, content}`.

  Requires `node` on PATH.
  """
  def js_preview(source_name, source_content, js_code) do
    js_fetch(source_name, source_content, js_code, @preview_cap)
  end

  def js_materialise(source_name, source_content, js_code, tournament_name) do
    with {:ok, rows} <- js_fetch(source_name, source_content, js_code, @max_entries) do
      materialise(rows, tournament_name)
    end
  end

  defp js_fetch(source_name, source_content, js_code, limit) do
    runner = """
    const input = {name: #{Jason.encode!(source_name)}, content: #{Jason.encode!(source_content)}};
    #{js_code}
    const out = process(input);
    if (!Array.isArray(out)) throw new Error("process() must return an array");
    const capped = out.slice(0, #{limit}).map(r => ({
      name: String(r.name || ""),
      content: String(r.content ?? "")
    }));
    process.stdout.write(JSON.stringify(capped));
    """

    case System.cmd("node", ["-e", runner], stderr_to_stdout: true) do
      {out, 0} ->
        with {:ok, list} when is_list(list) <- Jason.decode(out) do
          rows =
            Enum.map(list, fn %{"name" => n, "content" => c} -> {n, c} end)

          {:ok, rows}
        else
          _ -> {:error, "could not parse node output: #{String.slice(out, 0, 500)}"}
        end

      {out, code} ->
        {:error, "node exit #{code}: #{String.slice(out, 0, 1000)}"}
    end
  end

  # ── shared materialise ───────────────────────────────────────────────

  defp materialise(rows, tournament_name) do
    dir = Builder.upload_dir(tournament_name || "unnamed")
    File.mkdir_p!(dir)

    paths =
      rows
      |> Enum.with_index(1)
      |> Enum.map(fn {{name, content}, i} ->
        safe = sanitise(name, i)
        path = Path.join(dir, safe)
        content = content |> to_string() |> String.slice(0, @max_content_bytes)
        File.write!(path, content)
        path
      end)

    {:ok, paths}
  end

  defp sanitise(name, idx) do
    base =
      (name || "")
      |> to_string()
      |> String.replace(~r|[/\\]|, "_")
      |> String.trim()

    if base == "", do: "row_#{idx}.txt", else: base
  end

  defp normalise(rows) do
    Enum.map(rows, fn {name, content} ->
      %{name: to_string(name || ""), content: to_string(content || "")}
    end)
  end
end
