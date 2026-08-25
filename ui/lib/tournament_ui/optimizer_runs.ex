defmodule TournamentUi.OptimizerRuns do
  @moduledoc """
  Read-only Elixir adapter over the Python-side `optimizer_run` table in the
  central judgement-fabric DB.

  The Python optimizer runner writes rows here (see bin/optimizer_runs.py);
  the LiveView reads from here so it can show "still running", "done", logs,
  and result candidate version after the user comes back later.
  """

  alias Exqlite.Sqlite3

  defp db_path do
    home = System.get_env("DATA_TOURNAMENTS_HOME") || "/tmp/data-tournaments"
    Path.join(home, "judgements.db")
  end

  @doc """
  Create a `running` row and return its id.

  Mirrors `bin.optimizer_runs.start()` so the LiveView can create the
  persistent record before spawning the script that updates it.
  """
  def start(opts) do
    domain = Keyword.fetch!(opts, :domain)
    target = Keyword.fetch!(opts, :target)
    rubric = Keyword.get(opts, :rubric)
    prompt_name = Keyword.get(opts, :prompt_name)

    sql =
      "INSERT INTO optimizer_run(domain, target, rubric, prompt_name) " <>
        "VALUES (?, ?, ?, ?)"

    path = db_path()

    with {:ok, conn} <- Sqlite3.open(path),
         {:ok, st} <- Sqlite3.prepare(conn, sql),
         :ok <- Sqlite3.bind(st, [domain, target, rubric, prompt_name]),
         :done <- Sqlite3.step(conn, st),
         {:ok, id} <- Sqlite3.last_insert_rowid(conn) do
      Sqlite3.release(conn, st)
      Sqlite3.close(conn)
      id
    end
  end

  @doc """
  Return all optimizer runs for a domain, newest first.

  Each row is a map with atom keys:
    %{
      id: integer,
      domain: string,
      target: string,                 # "judge" | "generator"
      status: string,                 # "running" | "done" | "error" | "canceled"
      log: string,                    # accumulated stdout lines
      result: map | nil,              # decoded JSON
      rubric: string | nil,
      prompt_name: string | nil,
      exit_code: integer | nil,
      started_at: string,
      finished_at: string | nil
    }
  """
  def list_for_domain(domain) when is_binary(domain) do
    sql =
      "SELECT id, domain, target, rubric, prompt_name, status, exit_code, " <>
        "log, result, started_at, finished_at " <>
        "FROM optimizer_run WHERE domain = ? ORDER BY id DESC"

    case query(sql, [domain]) do
      {:ok, rows} -> Enum.map(rows, &row_to_map/1)
      _ -> []
    end
  end

  @doc """
  Return the most recent optimizer run matching domain + target.

  Returns `nil` if no run exists. Useful for the "Optimize judge" /
  "Optimize generator" buttons which need to know the current state to
  decide whether to render "Run now" or "Running… (last started 2 min ago)".
  """
  def latest(opts) do
    domain = Keyword.fetch!(opts, :domain)
    target = Keyword.fetch!(opts, :target)

    sql =
      "SELECT id, domain, target, rubric, prompt_name, status, exit_code, " <>
        "log, result, started_at, finished_at " <>
        "FROM optimizer_run WHERE domain = ? AND target = ? " <>
        "ORDER BY id DESC LIMIT 1"

    case query(sql, [domain, target]) do
      {:ok, [row | _]} -> row_to_map(row)
      _ -> nil
    end
  end

  # ── private ────────────────────────────────────────────────────────────

  defp row_to_map([
         id,
         domain,
         target,
         rubric,
         prompt_name,
         status,
         exit_code,
         log,
         result_json,
         started_at,
         finished_at
       ]) do
    %{
      id: id,
      domain: domain,
      target: target,
      rubric: rubric,
      prompt_name: prompt_name,
      status: status,
      exit_code: exit_code,
      log: log || "",
      result: decode_json(result_json),
      started_at: started_at,
      finished_at: finished_at
    }
  end

  defp decode_json(nil), do: nil
  defp decode_json(""), do: nil

  defp decode_json(s) when is_binary(s) do
    case Jason.decode(s) do
      {:ok, v} -> v
      _ -> nil
    end
  end

  defp query(sql, params) do
    path = db_path()

    case File.exists?(path) do
      false ->
        {:error, :no_db}

      true ->
        with {:ok, conn} <- Sqlite3.open(path),
             {:ok, st} <- Sqlite3.prepare(conn, sql),
             :ok <- Sqlite3.bind(st, params),
             {:ok, rows} <- fetch_all(conn, st, []) do
          Sqlite3.release(conn, st)
          Sqlite3.close(conn)
          {:ok, rows}
        end
    end
  end

  defp fetch_all(conn, st, acc) do
    case Sqlite3.step(conn, st) do
      {:row, row} -> fetch_all(conn, st, [row | acc])
      :done -> {:ok, Enum.reverse(acc)}
      err -> err
    end
  end
end
